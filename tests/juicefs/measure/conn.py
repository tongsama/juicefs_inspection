"""プロファイルから Redis への読み取り専用の接続を作る。"""
import os

from lib.resp import Resp, connect_from_script

# 本番の接続情報（MOUNT_FROM）を持つ mount スクリプト。内容は実行時に読むだけで表示しない。
MOUNT_SCRIPT = os.path.expanduser("~/utilities/cloudfs/juicefs_mount.sh")


def connect_profile(profile):
    """プロファイルの redis_source に従い、読み取り専用で接続する。
    mount_script は本番（MOUNT_FROM）、test は profile.redis_addr（host:port、認証なし）。"""
    if profile.redis_source == "mount_script":
        return connect_from_script(MOUNT_SCRIPT, readonly=True)
    if profile.redis_source == "test":
        host, _, port = profile.redis_addr.rpartition(":")
        return Resp(host, int(port), readonly=True)
    raise ValueError("未知の redis_source: %s" % profile.redis_source)
