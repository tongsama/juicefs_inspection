"""JuiceFS の `.accesslog` の 1 行を操作（Op）に解析する。

形式は pkg/vfs/vfs.go と accesslog.go の logit に合わせる（spec 5.1）。
  write      (ino,size,off,fh)
  read       (ino,size,off,fh): n   または  : (n)
  fsync      (ino,datasync)
  flush      (ino,fh,lockOwner)
  fallocate  (ino,mode,off,size)
  setattr    (ino[fh],0xMASK,[size=N,...]):...   size= を含むものだけ truncate
行の時刻は操作の終わりの時刻で、開始は終わり − <所要秒>。
"""
import dataclasses
import re
import time

# 行の頭: 時刻、[uid,gid,pid]、メソッド、本文、" - 状態"、<所要秒>
LINE_RE = re.compile(
    r"^(\d{4})\.(\d\d)\.(\d\d) (\d\d):(\d\d):(\d\d)\.(\d{6}) "
    r"\[uid:(\d+),gid:(\d+),pid:(\d+)\] (\w+) (.*) - (.*) <([0-9.]+)>$")
_WRITE_RE = re.compile(r"^\((\d+),(\d+),(\d+),(\d+)\)$")
_READ_RE = re.compile(r"^\((\d+),(\d+),(\d+),(\d+)\): \(?(\d+)\)?$")
_FSYNC_RE = re.compile(r"^\((\d+),(\d+)\)$")
_FLUSH_RE = re.compile(r"^\((\d+),(\d+),[0-9A-Fa-f]+\)$")
_FALLOC_RE = re.compile(r"^\((\d+),(\d+),(\d+),(\d+)\)$")
_SETATTR_RE = re.compile(r"^\((\d+)\[\d+\],0x([0-9A-Fa-f]+),\[([^\]]*)\]\)")
_SIZE_RE = re.compile(r"(?:^|,)size=(\d+)(?:,|$)")
KINDS = ("write", "read", "fsync", "flush", "fallocate", "truncate")
# 解析の対象のメソッド（これ以外のメソッドの行は、その inode のものでも対象外の None）
HANDLED = {"write", "read", "fsync", "flush", "fallocate", "setattr"}


@dataclasses.dataclass
class Op:
    """1 回の操作。時刻は UNIX マイクロ秒。read／write の size は要求の長さ、truncate の size は新しい長さ（off は 0）、
    fallocate の mode は mode、truncate の mode は setattr の MASK。status は logit の状態文字列（OK など）。"""
    start_us: int
    end_us: int
    pid: int
    kind: str
    off: int
    size: int
    mode: int
    dur_us: int
    status: str = "OK"


class Unknown:
    """解析できなかった行（形の違い）。数えて報告するために、元の行を持つ。"""

    def __init__(self, line):
        self.line = line

    def __repr__(self):
        return "Unknown(%r)" % self.line[:80]


def _epoch_us(m):
    """行頭の時刻（mount の process のローカル時刻）を UNIX マイクロ秒にする。"""
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    return int(time.mktime((y, mo, d, h, mi, s, 0, 0, -1))) * 1000000 + int(m.group(7))


def parse_line(line, inode):
    """1 行を解析する。対象の inode の write／read／fsync／flush／fallocate／truncate なら Op、
    別の inode や対象外のメソッドなら None、形が知らないものなら Unknown を返す。"""
    line = line.rstrip("\r\n")
    m = LINE_RE.match(line)
    if not m:
        return Unknown(line)
    method, body, status = m.group(11), m.group(12), m.group(13)
    if method not in HANDLED:
        return None
    pid, dur_us = int(m.group(10)), int(round(float(m.group(14)) * 1e6))
    end_us = _epoch_us(m)
    off = size = mode = 0
    if method == "write":
        a = _WRITE_RE.match(body)
        if not a:
            return Unknown(line)
        ino, size, off = int(a.group(1)), int(a.group(2)), int(a.group(3))
        kind = "write"
    elif method == "read":
        a = _READ_RE.match(body)
        if not a:
            return Unknown(line)
        ino, size, off = int(a.group(1)), int(a.group(2)), int(a.group(3))
        kind = "read"
    elif method == "fsync":
        a = _FSYNC_RE.match(body)
        if not a:
            return Unknown(line)
        ino, mode, kind = int(a.group(1)), int(a.group(2)), "fsync"
    elif method == "flush":
        a = _FLUSH_RE.match(body)
        if not a:
            return Unknown(line)
        ino, kind = int(a.group(1)), "flush"
    elif method == "fallocate":
        a = _FALLOC_RE.match(body)
        if not a:
            return Unknown(line)
        ino, mode, off, size = (int(a.group(i)) for i in range(1, 5))
        kind = "fallocate"
    else:  # setattr
        a = _SETATTR_RE.match(body)
        if not a:
            return Unknown(line)
        ino, mode = int(a.group(1)), int(a.group(2), 16)
        sm = _SIZE_RE.search(a.group(3))
        if not sm:
            # size を変えない setattr（chmod／chown／utimens）は対象外
            return None
        size, kind = int(sm.group(1)), "truncate"
    if ino != inode:
        return None
    return Op(end_us - dur_us, end_us, pid, kind, off, size, mode, dur_us, status)
