"""profiles.toml からプロファイル（本番 prod／試験 test）を読む。"""
import dataclasses
import os
import tomllib

PROFILES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles.toml")


@dataclasses.dataclass(frozen=True)
class Profile:
    """計測の対象 1 つ分の設定。redis_source は "mount_script"（本番）か "test"（試験）。"""
    name: str
    metrics_url: str
    inode: int
    cache_dir: str
    mountpoint: str
    redis_source: str
    redis_addr: str
    log_glob: str
    bucket: str = ""


def _load(path=None):
    with open(path or PROFILES_PATH, "rb") as f:
        return tomllib.load(f)


def load_profile(name, path=None):
    """名前のプロファイルを返す。~ は展開する。未知の名前は KeyError。"""
    d = _load(path)
    if name not in d or name == "common":
        raise KeyError("未知のプロファイル: %s" % name)
    p = dict(d[name])
    for k in ("cache_dir", "mountpoint", "log_glob"):
        p[k] = os.path.expanduser(p[k])
    return Profile(name=name, **p)


def load_common(path=None):
    """共通の資源と上限（redis_server、binary、間隔など）を dict で返す。~ は展開する。"""
    c = dict(_load(path)["common"])
    for k in ("redis_server", "test_binary"):
        c[k] = os.path.expanduser(c[k])
    return c
