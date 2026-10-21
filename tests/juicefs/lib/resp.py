"""パイプライン対応の最小限の Redis（RESP2）クライアント。readonly では読み取りの命令だけを許す。"""
import socket
from urllib.parse import unquote, urlparse

# 本番に送ってよい読み取りの命令（接続の確立に要る AUTH／SELECT を含む）
READONLY_COMMANDS = frozenset({"LLEN", "LRANGE", "HGET", "DBSIZE", "SCAN", "AUTH", "SELECT"})


class Resp:
    """RESP2 クライアント。readonly=True のとき許可外の命令は送る前に PermissionError。"""

    def __init__(self, host, port, password=None, db=0, readonly=True, timeout=30, sock=None):
        """sock を渡すと接続せずにそれを使う（試験用）。password は文字列か Secret。"""
        self.readonly = readonly
        self.sock = sock if sock is not None else socket.create_connection((host, port), timeout=timeout)
        self.buf = self.sock.makefile("rb", buffering=1 << 20)
        try:
            if password is not None:
                self.call("AUTH", getattr(password, "value", password))
            if db:
                self.call("SELECT", db)
        except BaseException:  # AUTH／SELECT の失敗では、接続を漏らさず閉じてから投げ直す
            self.close()
            raise

    def close(self):
        """接続を閉じる（何度呼んでもよい）。"""
        for obj in (self.buf, self.sock):
            try:
                obj.close()
            except OSError:
                pass

    def __enter__(self):
        """with 文で使えるようにする。"""
        return self

    def __exit__(self, *exc):
        """with 文を抜けるとき接続を閉じる。"""
        self.close()
        return False

    def _check(self, cmd):
        """readonly で許可外の命令なら PermissionError（引数の値は含めない）。"""
        if self.readonly:
            name = (cmd[0].decode() if isinstance(cmd[0], bytes) else str(cmd[0])).upper()
            if name not in READONLY_COMMANDS:
                raise PermissionError("readonly 接続で許可されない命令: %s" % name)

    def send(self, *cmds):
        """複数のコマンドをまとめて送る。"""
        for cmd in cmds:
            self._check(cmd)
        out = bytearray()
        for cmd in cmds:
            out += b"*%d\r\n" % len(cmd)
            for a in cmd:
                a = a if isinstance(a, bytes) else str(a).encode()
                out += b"$%d\r\n%s\r\n" % (len(a), a)
        self.sock.sendall(out)

    def read(self):
        """応答を 1 つ読む。"""
        line = self.buf.readline()
        if not line:
            raise ConnectionError("redis の接続が閉じられた")
        t, rest = line[:1], line[1:-2]
        if t == b"+":
            return rest
        if t == b"-":
            raise RuntimeError("redis error: " + rest.decode(errors="replace"))
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            return self.buf.read(n + 2)[:-2]
        if t == b"*":
            n = int(rest)
            return None if n < 0 else [self.read() for _ in range(n)]
        raise RuntimeError("unexpected reply type %r" % line[:20])

    def call(self, *cmd):
        """コマンドを 1 つ送って応答を返す。"""
        self.send(cmd)
        return self.read()


def connect_from_url(url, readonly=True):
    """redis:// の URL（str か Secret）で接続する。URL や認証情報は表示しない。"""
    try:
        u = urlparse(getattr(url, "value", url))
        host, port = u.hostname, u.port or 6379
    except ValueError:  # 標準ライブラリの例外の文言には URL の一部が入るので、固定の文言に替える
        raise ValueError("redis の URL の形式（ホスト、ポート）が不正") from None
    r = Resp(host, port, readonly=readonly)
    try:
        if u.password is not None:
            if u.username:
                r.call("AUTH", unquote(u.username), unquote(u.password))
            else:
                r.call("AUTH", unquote(u.password))
        elif u.username:
            r.call("AUTH", unquote(u.username))
        r.call("SELECT", (u.path or "/0").strip("/") or "0")
    except BaseException:
        r.close()
        raise
    return r


def connect_from_script(script, readonly=True):
    """mount スクリプトの MOUNT_FROM で接続する。"""
    from .secrets import redis_url_from_mount_script
    return connect_from_url(redis_url_from_mount_script(script), readonly=readonly)
