"""RTT を足す TCP の中継（試験の Redis の手前に置き、本番の約 10 ms の往復を再現する）。

往路と復路それぞれで、受け取った塊を one_way_ms だけ遅らせ、受け取った順に送る。pipeline の命令は
1 つの塊で届くのでまとめて遅れ、往復 1 回分しか増えない（本番と同じく pipeline の効果が残る）。

標準ライブラリだけで書く（`python3 -I <この file>` で常駐させるため、パッケージの import をしない）。
常駐: python3 -I replay/rtt_proxy.py --listen 127.0.0.1:16380 --target 127.0.0.1:16379 --one-way-ms 5"""
import argparse
import asyncio
import signal
import socket
import sys


def parse_addr(s):
    """"host:port" を (host, port) にする。port が無い・整数でないときは ValueError。"""
    host, sep, port = s.rpartition(":")
    if not sep or not host:
        raise ValueError("host:port の形ではない: %s" % s)
    return host, int(port)


async def _pump(reader, writer, delay):
    """reader から読んだ塊を、受け取った時刻 + delay 秒に writer へ書く。順序は保つ。
    読む側と書く側を別の task にして、遅らせている間も次の塊を受け取れるようにする。"""
    loop = asyncio.get_running_loop()
    q = asyncio.Queue()

    async def sender():
        """queue の塊を、それぞれの送る時刻まで待ってから書く。None で終わる。"""
        while True:
            item = await q.get()
            if item is None:
                break
            due, data = item
            wait = due - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            writer.write(data)
            await writer.drain()

    st = asyncio.create_task(sender())
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            q.put_nowait((loop.time() + delay, data))
    except (ConnectionError, OSError):
        pass
    finally:
        q.put_nowait(None)
        try:
            await st
        except (ConnectionError, OSError):
            pass
        try:
            if writer.can_write_eof():
                writer.write_eof()  # 片側の終わりを相手へ伝える（遅らせた塊を送り切った後）
        except (ConnectionError, OSError):
            pass


def _nodelay(writer):
    """Nagle を切る（小さな塊を遅らせずに送るため）。"""
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


async def start(listen, target, one_way_ms):
    """中継を listen（"host:port"）で始め、asyncio.Server を返す。接続ごとに target へつなぎ、両方向を遅らせる。"""
    lhost, lport = parse_addr(listen)
    thost, tport = parse_addr(target)
    delay = one_way_ms / 1000.0

    async def handle(cr, cw):
        """1 つの接続の中継。どちらかが閉じたら両方を閉じる。"""
        _nodelay(cw)
        try:
            tr, tw = await asyncio.open_connection(thost, tport)
        except OSError:
            cw.close()
            return
        _nodelay(tw)
        try:
            await asyncio.gather(_pump(cr, tw, delay), _pump(tr, cw, delay))
        finally:
            for w in (cw, tw):
                w.close()

    return await asyncio.start_server(handle, lhost, lport, reuse_address=True)


async def serve(listen, target, one_way_ms):
    """中継を始めて、止められるまで動かす（常駐用）。"""
    srv = await start(listen, target, one_way_ms)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, stop.set)
    await stop.wait()
    srv.close()
    srv.close_clients()  # 残りの接続の task は asyncio.run が終わるときに取り消す


def main(argv=None):
    """常駐の入口。"""
    ap = argparse.ArgumentParser(description="RTT を足す TCP の中継")
    ap.add_argument("--listen", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--one-way-ms", type=float, default=5.0)
    a = ap.parse_args(argv)
    print("中継 %s → %s、片道 %.1f ms" % (a.listen, a.target, a.one_way_ms), flush=True)
    asyncio.run(serve(a.listen, a.target, a.one_way_ms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
