"""mount／rclone のスクリプトから接続情報を実行時に読む。値は Secret に包み、表示・例外・ログに出さない。"""
import re


class Secret:
    """値を隠す小さな型。str／repr／format／logging のどれでも <redacted> になり、実値は .value だけで取れる。"""

    __slots__ = ("_v",)

    def __init__(self, value):
        object.__setattr__(self, "_v", value)

    @property
    def value(self):
        """本当の値（明示的に取り出すときだけ使う）。"""
        return self._v

    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__

    def __format__(self, spec):
        return "<redacted>"

    def __setattr__(self, k, v):
        raise AttributeError("Secret は変更できない")

    def __reduce__(self):
        raise TypeError("Secret は直列化できない")


def _shell_assignments(path):
    """シェルスクリプトの NAME=値 / export NAME="値" の行を dict にする（最後の定義が勝つ）。"""
    out = {}
    pat = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"]*)"|'([^']*)'|(\S*))""")
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = pat.match(line)
            if m:
                out[m.group(1)] = next(g for g in m.groups()[1:] if g is not None)
    return out


def redis_url_from_mount_script(path):
    """mount スクリプトの MOUNT_FROM（${...} の展開部分は除く）を Secret で返す。無ければ ValueError（値は含めない）。"""
    v = _shell_assignments(path).get("MOUNT_FROM")
    if not v:
        raise ValueError("mount スクリプトに MOUNT_FROM が見つからない")
    return Secret(re.sub(r"\$\{[^}]*\}", "", v))


def rclone_keys(path):
    """rclone スクリプトから (access_key, secret, port) を返す。鍵は Secret、port は int。"""
    a = _shell_assignments(path)
    try:
        return (Secret(a["RCLONE_SERVE_S3_ACCESS_KEY"]), Secret(a["RCLONE_SERVE_S3_SECRET"]),
                int(a["RCLONE_SERVE_S3_PORT"]))
    except KeyError as e:
        raise ValueError("rclone スクリプトに %s が見つからない" % e.args[0]) from None
    except ValueError:
        raise ValueError("RCLONE_SERVE_S3_PORT が整数ではない") from None
