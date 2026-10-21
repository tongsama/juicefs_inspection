"""記録（trace TSV）の書き出しと読み込み。

列: start_us end_us pid kind off size mode dur_us status（タブ区切り、1 行 1 操作、記録の順）。
"""
from replay.accesslog import KINDS, Op


def format_op(op):
    """Op を TSV の 1 行（改行付き）にする。"""
    return "%d\t%d\t%d\t%s\t%d\t%d\t%d\t%d\t%s\n" % (
        op.start_us, op.end_us, op.pid, op.kind, op.off, op.size, op.mode, op.dur_us, op.status)


def load_unsorted(path):
    """TSV を記録の順のまま Op のリストにする。壊れた行は飛ばす。"""
    ops = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 9 or p[3] not in KINDS:
                continue
            try:
                ops.append(Op(int(p[0]), int(p[1]), int(p[2]), p[3], int(p[4]), int(p[5]), int(p[6]), int(p[7]), p[8]))
            except ValueError:
                pass
    return ops


def load(path):
    """開始の時刻で並べた Op のリスト。同じ pid の操作は、並べ替えた後の位置の集まりの中で記録の順を保つ
    （複数 thread で終了時刻が前後して記録された行があっても、pid ごとの順序は変えない）。"""
    ops = load_unsorted(path)
    order = sorted(range(len(ops)), key=lambda i: ops[i].start_us)  # 安定ソート
    slots = {}  # pid → 並べ替え後の位置の昇順
    for pos, i in enumerate(order):
        slots.setdefault(ops[i].pid, []).append(pos)
    result = [None] * len(ops)
    seq = {}
    for i, op in enumerate(ops):  # 記録の順に、その pid の位置を前から割り当てる
        k = seq.get(op.pid, 0)
        result[slots[op.pid][k]] = op
        seq[op.pid] = k + 1
    return result


def head(ops, seconds):
    """開始の時刻で並べた ops（load の戻り値）のうち、最初の Op から seconds 秒未満に始まる Op だけを返す
    （jfsreplay run／suite の --head-seconds。短い時間で流し直しの時刻の精度を確かめるため）。
    seconds が None なら全部。0 以下は ValueError。"""
    if seconds is None:
        return list(ops)
    if seconds <= 0:
        raise ValueError("--head-seconds は正の数: %r" % (seconds,))
    if not ops:
        return []
    limit = ops[0].start_us + seconds * 1e6
    return [o for o in ops if o.start_us < limit]
