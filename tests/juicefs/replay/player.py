"""記録（trace の Op）を、記録の開始の時刻の間隔どおりにファイルへ流し直す。

- pid（記録の上の qemu の thread）ごとに thread を作り、各 Op を t0 + (start_us − 最初の start_us) の時刻に出す。
  同じ pid の Op は記録の順に 1 つずつ出す（前の Op が遅れたら、次はその後すぐ出す）。
- 呼び出し: write は os.pwrite、read は os.pread、fsync は os.fsync、fallocate は fallocate(2)（ctypes）、
  truncate は os.ftruncate。flush は close のときだけ出るので、流し直しでは最後の close で代える（途中では何もしない）。
- ファイルは普通の O_RDWR で 1 回だけ開き、全 thread で共有する（位置付きの呼び出しなので共有してよい）。
- write の中身は seed.content と同じ作り方で、位置の値に 1 を足したもの（種と違う中身にして、本当に書き換える）。
- EIO などのエラーが 1 つでも出たら、その時点で全 thread を止める（失敗を隠さない。Review Focus 4）。
- 記録の上で失敗していた（status が OK でない）Op は流さず、skipped に数える。"""
import ctypes
import ctypes.util
import dataclasses
import errno
import os
import threading
import time

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

    @property
    def failed(self):
        """エラーが 1 つでもあれば失敗。"""
        return bool(self.errors)

    def to_dict(self):
        """play.json に書く dict。"""
        d = dataclasses.asdict(self)
        d["failed"] = self.failed
        return d


def _do(fd, op):
    """Op を 1 つ実行する。失敗は例外のまま上げる。"""
    k = op.kind
    if k == "write":
        data = memoryview(content(op.off, op.size))
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


def _error(op, e, t0):
    """例外をエラーの一覧の 1 項目にする。"""
    code = getattr(e, "errno", None)
    return {"pid": op.pid if op else None, "kind": op.kind if op else "close", "off": op.off if op else None,
            "size": op.size if op else None, "errno": errno.errorcode.get(code, code) if code else None,
            "message": str(e), "t_s": round(time.monotonic() - t0, 6)}


def play(path, ops, t0, stop=None, clock=time.monotonic):
    """path を O_RDWR で開き、ops（開始の時刻順）を pid ごとの thread で流す。t0 は clock() の値の基準の時刻。
    stop（threading.Event）が立つか、エラーが出たら、全 thread が次の Op の前で止まる。戻り値は PlayResult。"""
    stop = stop or threading.Event()
    todo = [o for o in ops if o.status == "OK"]
    skipped = len(ops) - len(todo)
    base = todo[0].start_us if todo else 0
    by_pid = {}
    for o in todo:
        by_pid.setdefault(o.pid, []).append(o)
    lat, lags, errors = {}, [0.0], []
    lock = threading.Lock()
    done = [0]
    reason = []
    fd = os.open(path, os.O_RDWR)

    def worker(seq):
        """1 つの pid の Op を予定の時刻に順に出す。"""
        for op in seq:
            when = t0 + (op.start_us - base) / 1e6
            while True:
                left = when - clock()
                if left <= 0 or stop.is_set():
                    break
                stop.wait(min(left, 0.05) if left > 0.002 else 0)  # 最後の 2 ms は回して待つ（精度のため）
            if stop.is_set():
                return
            a = clock()
            try:
                _do(fd, op)
            except Exception as e:  # EIO など。その時点で全体を止める
                with lock:
                    errors.append(_error(op, e, t0))
                    reason.append("%s で %s" % (op.kind, e))
                stop.set()
                return
            b = clock()
            with lock:
                lat.setdefault(op.kind, []).append(b - a)
                lags.append(a - when)
                done[0] += 1

    started = clock()
    threads = [threading.Thread(target=worker, args=(seq,), name="replay-pid-%d" % pid, daemon=True)
               for pid, seq in by_pid.items()]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        a = clock()
        try:
            os.close(fd)  # flush の代わり。close の失敗（書き出しの失敗）も隠さない
            lat.setdefault("flush", []).append(clock() - a)
        except OSError as e:
            errors.append(_error(None, e, t0))
            reason.append("close で %s" % e)
    stopped = stop.is_set() or bool(errors)
    if stopped and not reason:
        reason.append("外から止められた")
    return PlayResult(latency={k: summarize(v) for k, v in sorted(lat.items())}, max_lag_ms=max(lags) * 1000,
                      errors=errors, ops_total=len(ops), ops_done=done[0], skipped=skipped, stopped=stopped,
                      duration_s=clock() - started, stop_reason="; ".join(reason))
