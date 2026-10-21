"""記録（trace の Op）を、記録の開始の時刻の間隔どおりにファイルへ流し直す。

- pid（記録の上の qemu の thread）ごとに thread を作り、各 Op を t0 + (start_us − 最初の start_us) の時刻に出す。
  同じ pid の Op は記録の順に 1 つずつ出す（前の Op が遅れたら、次はその後すぐ出す）。
- pid は複数の worker の process（既定 min(16, pid の数)）に、pid の安定な hash（crc32）で分ける。各 process は
  自分の fd と、受け持つ pid ごとの thread を持ち、switch interval を小さく（0.5 ms）する（Ruling 12）。
  1 つの process に 163 thread を置くと、山の時刻に多くの thread が同時に動けるようになり、syscall の後の GIL の
  取り直し（switch interval 5 ms）で、実の試験の mount で write の p50 が 409 ms、遅れが 29 秒になったため。
  時刻は全 process で共通の CLOCK_MONOTONIC（Linux では system 全体で同じ）で合わせる。
- 呼び出し: write は os.pwrite、read は os.pread、fsync は os.fsync、fallocate は fallocate(2)（ctypes）、
  truncate は os.ftruncate。flush は close のときだけ出るので、流し直しでは最後の close で代える（途中では何もしない）。
- ファイルは process ごとに普通の O_RDWR で 1 回だけ開き、その process の全 thread で共有する（位置付きの呼び出しなので共有してよい）。
- write の中身は seed.content と同じ作り方で、位置の値に 1 を足したもの（種と違う中身にして、本当に書き換える）。
- EIO などのエラーが 1 つでも出たら、その時点で全 process の全 thread を止める（失敗を隠さない。Review Focus 4）。
- 記録の上で失敗していた（status が OK でない）Op は流さず、skipped に数える。"""
import ctypes
import ctypes.util
import dataclasses
import errno
import multiprocessing
import multiprocessing.connection
import os
import sys
import threading
import time
import zlib

from replay.seed import PAGE

_libc = None


def content(off, n):
    """位置 off から n バイトの、流し直しの write の中身。4 KiB ごとに（その 4 KiB の先頭位置 + 1）を
    8 バイト big endian で 1 回書き、残りは 0。seed.content と同じ形で値だけ違う。"""
    start = off - off % PAGE
    end = off + n
    pages = [(a + 1).to_bytes(8, "big") + bytes(PAGE - 8) for a in range(start, end, PAGE)]
    return b"".join(pages)[off - start:off - start + n]


def fallocate(fd, mode, off, length):
    """fallocate(2) を ctypes で呼ぶ。失敗は OSError（errno 付き）。"""
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        _libc.fallocate.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong)
    if _libc.fallocate(fd, mode, off, length) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


def summarize(secs):
    """待ち時間（秒）のリストから件数・中央値・99%・最大（ミリ秒）を出す。順位は nearest-rank。"""
    s = sorted(secs)
    n = len(s)
    if not n:
        return {"count": 0, "p50_ms": None, "p99_ms": None, "max_ms": None}

    def rank(p):
        return s[max(0, -(-n * p // 100) - 1)] * 1000
    return {"count": n, "p50_ms": rank(50), "p99_ms": rank(99), "max_ms": s[-1] * 1000}


@dataclasses.dataclass
class PlayResult:
    """流し直しの結果。latency は kind → summarize の dict、max_lag_ms は予定の時刻からの遅れの最大、
    errors はエラーの一覧（pid、kind、off、size、errno、message、t_s）。stopped は外からの停止かエラーで途中で止まったか。"""
    latency: dict
    max_lag_ms: float
    errors: list
    ops_total: int
    ops_done: int
    skipped: int
    stopped: bool
    duration_s: float
    stop_reason: str = ""
    procs: int = 0  # worker の process の数（0 はこの process の thread だけ）
    lag: dict = dataclasses.field(default_factory=dict)  # 予定の時刻からの遅れの summarize（count、p50、p99、max の ms）

    @property
    def failed(self):
        """エラーが 1 つでもあれば失敗。"""
        return bool(self.errors)

    def to_dict(self):
        """play.json に書く dict。"""
        d = dataclasses.asdict(self)
        d["failed"] = self.failed
        return d


def _do(fd, op, data=None):
    """Op を 1 つ実行する。write の中身 data は呼ぶ側が先に作って渡す（待ち時間に中身を作る時間を入れないため）。
    失敗は例外のまま上げる。"""
    k = op.kind
    if k == "write":
        data = memoryview(content(op.off, op.size) if data is None else data)
        at = op.off
        while len(data):
            n = os.pwrite(fd, data, at)
            if n <= 0:
                raise OSError(errno.EIO, "pwrite が 0 バイトを返した")
            data, at = data[n:], at + n
    elif k == "read":
        os.pread(fd, op.size, op.off)
    elif k == "fsync":
        os.fsync(fd)
    elif k == "fallocate":
        fallocate(fd, op.mode, op.off, op.size)
    elif k == "truncate":
        os.ftruncate(fd, op.size)
    # flush は最後の close で代える（ここでは何もしない）


def _error(op, e, t0, clock=time.monotonic):
    """例外をエラーの一覧の 1 項目にする（t_s は clock の値の t0 からの秒）。"""
    code = getattr(e, "errno", None)
    return {"pid": op.pid if op else None, "kind": op.kind if op else "close", "off": op.off if op else None,
            "size": op.size if op else None, "errno": errno.errorcode.get(code, code) if code else None,
            "message": str(e), "t_s": round(clock() - t0, 6)}


ISSUE_EARLY_S = 0.0005  # 残りがこれ未満なら待たずに出す（眠りの起き遅れの方が大きいため）
MAX_SLEEP_S = 1.0  # 1 回の眠りの上限（clock を差し替えた試験でも予定を見直せるように）


def _sleep_until(when, stop, clock):
    """clock() が when になるまで眠って待つ。stop.wait（timeout 付き）で眠るので、待つ間は GIL を離し、
    stop が立てばすぐ起きる。回して待つ（busy-wait）ことはしない（初回の本番規模の試験で、163 thread が
    最後の 2 ms を回して GIL を取り合い、write の p50 が 834 ms になったため）。残りが ISSUE_EARLY_S 未満なら戻る。"""
    while not stop.is_set():
        left = when - clock()
        if left < ISSUE_EARLY_S:
            return
        stop.wait(min(left, MAX_SLEEP_S))


PROCS_MAX = 16  # worker の process の数の上限
WORKER_SWITCH_S = 0.0005  # worker の process の sys.setswitchinterval（GIL の取り直しの待ちを短くする）
READY_TIMEOUT_S = 120.0  # worker の process が準備できるまで待つ上限
START_MARGIN_S = 0.05  # 準備が t0 に間に合わなかったとき、今からこれだけ後に始める


def _mono():
    """全 process で共通の時計（CLOCK_MONOTONIC。Linux では system 全体で同じ値）。"""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def _engine(path, seqs, t0, base, stop, clock, on_error=None):
    """path を O_RDWR で開き、seqs（pid → その pid の Op の list）を pid ごとの thread で、t0 + (start_us − base) の
    時刻（clock の値）に流す。stop（threading.Event）が立つかエラーが出たら、全 thread が次の Op の前で止まる。
    エラーのときは on_error も呼ぶ（他の process を止めるため）。最後に fd を閉じる（flush の代わり）。
    戻り値は {lat: kind → 秒の list, lags: 秒の list, errors, reasons, done}。"""
    lat, lags, errors, reason = {}, [0.0], [], []
    lock = threading.Lock()
    done = [0]
    fd = os.open(path, os.O_RDWR)

    def worker(seq):
        """1 つの pid の Op を予定の時刻に順に出す。"""
        for op in seq:
            when = t0 + (op.start_us - base) / 1e6
            data = content(op.off, op.size) if op.kind == "write" else None  # 待つ前に作る（遅れにも待ち時間にも入れない）
            _sleep_until(when, stop, clock)
            if stop.is_set():
                return
            a = clock()
            try:
                _do(fd, op, data)
            except Exception as e:  # EIO など。その時点で全体を止める
                with lock:
                    errors.append(_error(op, e, t0, clock))
                    reason.append("%s で %s" % (op.kind, e))
                stop.set()
                if on_error:
                    on_error()
                return
            b = clock()
            with lock:
                lat.setdefault(op.kind, []).append(b - a)
                lags.append(a - when)
                done[0] += 1

    threads = [threading.Thread(target=worker, args=(seq,), name="replay-pid-%d" % pid, daemon=True)
               for pid, seq in seqs.items()]
    try:
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        except BaseException:  # 中断（KeyboardInterrupt など）: 全 thread を止めて待ってから fd を閉じる
            stop.set()
            for t in threads:
                if t.is_alive():
                    t.join()
            raise
    finally:
        a = clock()
        try:
            os.close(fd)  # flush の代わり。close の失敗（書き出しの失敗）も隠さない
            lat.setdefault("flush", []).append(clock() - a)
        except OSError as e:
            errors.append(_error(None, e, t0, clock))
            reason.append("close で %s" % e)
            if on_error:
                on_error()
    return {"lat": lat, "lags": lags, "errors": errors, "reasons": reason, "done": done[0]}


def _proc_main(conn, path, seqs, switch_s):
    """worker の process の本体。準備ができたら "ready" を送り、("go", t0, base) を待って _engine で流し、
    結果の dict を送る。("stop",) なら何もせずに空の結果を送る。流している間は、親からの ("stop",)（外からの停止や
    他の process のエラー）か pipe の切断で自分の thread を止め、自分のエラーでは ("error",) を親に送る。
    process をまたぐ停止に multiprocessing.Event を使わないのは、待っている thread が lock を持ったまま process が
    終わると、親の set／is_set が永久に止まることがあるため（試験で再現した）。"""
    sys.setswitchinterval(switch_s)
    local = threading.Event()
    send_lock = threading.Lock()
    conn.send("ready")
    msg = conn.recv()
    if msg[0] != "go":
        conn.send({"lat": {}, "lags": [0.0], "errors": [], "reasons": [], "done": 0})
        return

    def listen():
        """親からの停止（か切断）を待ち、この process の停止に写す。"""
        try:
            conn.recv()
        except (EOFError, OSError):
            pass
        local.set()

    def on_error():
        """自分のエラーを親に知らせる（親が全 process を止める）。"""
        with send_lock:
            try:
                conn.send(("error",))
            except OSError:
                pass
    threading.Thread(target=listen, name="replay-stop-listen", daemon=True).start()
    try:
        res = _engine(path, seqs, msg[1], msg[2], local, _mono, on_error=on_error)
    except BaseException as e:  # 開けない・想定外: 結果として返し、全体を止める
        res = {"lat": {}, "lags": [0.0], "errors": [_error(None, e, msg[1], _mono)],
               "reasons": ["worker で %s: %s" % (type(e).__name__, e)], "done": 0}
    with send_lock:
        conn.send(res)


def _groups(by_pid, n):
    """pid を n 個の組に、pid の安定な hash（crc32）で分ける（同じ pid は必ず同じ組。空の組は除く）。"""
    gs = [{} for _ in range(n)]
    for pid, seq in by_pid.items():
        gs[zlib.crc32(str(pid).encode()) % n][pid] = seq
    return [g for g in gs if g]


def _merge(parts):
    """process ごとの結果を 1 つにまとめる。"""
    out = {"lat": {}, "lags": [0.0], "errors": [], "reasons": [], "done": 0}
    for r in parts:
        for k, v in r["lat"].items():
            out["lat"].setdefault(k, []).extend(v)
        out["lags"].extend(r["lags"])
        out["errors"].extend(r["errors"])
        out["reasons"].extend(r["reasons"])
        out["done"] += r["done"]
    return out


def _play_procs(path, by_pid, t0, base, stop, clock, nproc):
    """by_pid を nproc 個の worker の process（spawn）で流す。t0 は clock の値で、共通の時計（_mono）に直して渡す。
    全 process の準備が t0 に間に合わなければ、準備ができてから START_MARGIN_S 後に始める。
    外の stop が立つか、どこかの process から ("error",) が来れば、全 process に ("stop",) を送る（エラーなら外の stop も立てる）。"""
    ctx = multiprocessing.get_context("spawn")
    procs, conns = [], []
    results = {}
    stopping = [False]

    def stop_all():
        """全 process に停止を送る（1 回だけ。終わった process への送りの失敗は無視）。"""
        if stopping[0]:
            return
        stopping[0] = True
        for c in conns:
            try:
                c.send(("stop",))
            except OSError:
                pass
    try:
        for g in _groups(by_pid, nproc):
            a, b = ctx.Pipe()
            p = ctx.Process(target=_proc_main, args=(b, path, g, WORKER_SWITCH_S), daemon=True, name="replay-worker")
            p.start()
            b.close()
            procs.append(p)
            conns.append(a)
        deadline = time.monotonic() + READY_TIMEOUT_S
        for c, p in zip(conns, procs):
            while not c.poll(0.05):
                if not p.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("流し直しの worker の process が準備できない（exitcode %s）" % p.exitcode)
            if c.recv() != "ready":
                raise RuntimeError("流し直しの worker の process から想定外の応答")
        if stop.is_set():
            stop_all()
        else:
            target, now = t0 - clock() + _mono(), _mono()
            start = target if target > now else now + START_MARGIN_S  # 準備が間に合わなければ少し後に始める
            for c in conns:
                c.send(("go", start, base))
        pending = dict(zip(conns, procs))
        while pending:
            if stop.is_set():
                stop_all()
            for c in multiprocessing.connection.wait(list(pending), timeout=0.05):
                try:
                    m = c.recv()
                except EOFError:  # 結果を送らずに終わった（落ちた）
                    p = pending[c]
                    p.join(5)
                    m = {"lat": {}, "lags": [0.0], "errors": [
                        {"pid": None, "kind": "worker", "off": None, "size": None, "errno": None,
                         "message": "worker の process が結果を送らずに終わった（exitcode %s）" % p.exitcode,
                         "t_s": None}], "reasons": ["worker の process が落ちた"], "done": 0}
                if isinstance(m, tuple):  # ("error",): その process でエラー。全体を止める
                    stop.set()
                    stop_all()
                    continue
                results[c] = m
                if m["errors"]:
                    stop.set()
                    stop_all()
                pending.pop(c)
    except BaseException:  # 中断や準備の失敗: 全 process を止めてから上げる
        stop_all()
        raise
    finally:
        for p in procs:
            p.join(10)
            if p.is_alive():
                p.terminate()
                p.join(5)
        for c in conns:
            c.close()
    return _merge(results.values())


def play(path, ops, t0, stop=None, clock=time.monotonic, procs=None):
    """path に ops（開始の時刻順）を流す。t0 は clock() の値の基準の時刻。stop（threading.Event）が立つか、
    エラーが出たら、全 thread が次の Op の前で止まる（エラーのときは stop も立つ）。戻り値は PlayResult。
    procs は worker の process の数（None なら min(PROCS_MAX, pid の数)）。0 ならこの process の thread だけで流す
    （os の関数を mock で差し替える試験用。GIL の取り合いがあるので本番規模の流し直しには使わない）。"""
    stop = stop or threading.Event()
    todo = [o for o in ops if o.status == "OK"]
    skipped = len(ops) - len(todo)
    base = todo[0].start_us if todo else 0
    by_pid = {}
    for o in todo:
        by_pid.setdefault(o.pid, []).append(o)
    nproc = min(PROCS_MAX, len(by_pid)) if procs is None else min(procs, len(by_pid))
    started = clock()
    if nproc <= 0:
        r = _engine(path, by_pid, t0, base, stop, clock)
    else:
        r = _play_procs(path, by_pid, t0, base, stop, clock, nproc)
    errors, reason = r["errors"], r["reasons"]
    stopped = stop.is_set() or bool(errors)
    if stopped and not reason:
        reason.append("外から止められた")
    return PlayResult(latency={k: summarize(v) for k, v in sorted(r["lat"].items())}, max_lag_ms=max(r["lags"]) * 1000,
                      errors=errors, ops_total=len(ops), ops_done=r["done"], skipped=skipped, stopped=stopped,
                      duration_s=clock() - started, stop_reason="; ".join(reason), procs=max(nproc, 0),
                      lag=summarize(r["lags"][1:]))
