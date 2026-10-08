#!/usr/bin/env python3
"""Serve a directory over HTTP for the install.sh tests.

Paths starting with /ratelimit/ answer 403 like the GitHub API when its
unauthenticated rate limit is used up; query strings are ignored.
"""
import functools
import http.server
import sys


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files, plus a forced 403 under /ratelimit/."""

    def do_GET(self):
        if self.path.startswith("/ratelimit/"):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"message":"API rate limit exceeded"}')
            return
        self.path = self.path.split("?", 1)[0]
        super().do_GET()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port, root = int(sys.argv[1]), sys.argv[2]
    handler = functools.partial(Handler, directory=root)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
