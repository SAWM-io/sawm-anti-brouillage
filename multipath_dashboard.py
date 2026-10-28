#!/usr/bin/env python3
"""Read-only localhost viewer for a fixed multipath evidence directory.

No browser command launches a process or changes the experiment. The laboratory
writes JSON atomically; this server exposes only four named JSON proof files.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit

FILES = frozenset({'progress.json', 'report.json', 'attempts.json', 'phase-events.json'})
MAX_BYTES = 32 * 1024 * 1024


class Viewer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, directory):
        self.directory = Path(directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError('data_dir_must_be_directory')
        self.page = Path(__file__).with_suffix('.html').read_bytes()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, content, kind='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(content)

    def do_GET(self):
        port = self.server.server_address[1]
        if self.headers.get('Host') not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
            return self.reply(403, b'{"error":"invalid_host"}')
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            return self.reply(404, b'{"error":"not_found"}')
        if parsed.path == '/':
            return self.reply(200, self.server.page, 'text/html; charset=utf-8')
        name = parsed.path.removeprefix('/')
        if name not in FILES:
            return self.reply(404, b'{"error":"not_found"}')
        path = self.server.directory / name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                return self.reply(404, b'{"error":"evidence_unavailable"}')
            data = path.read_bytes()
            if len(data) > MAX_BYTES:
                return self.reply(413, b'{"error":"evidence_too_large"}')
            json.loads(data)
            return self.reply(200, data)
        except (OSError, ValueError):
            return self.reply(404, b'{"error":"evidence_unavailable"}')

    do_HEAD = do_GET

    def do_POST(self):
        self.reply(405, b'{"error":"read_only"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8895)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024..65535')
    with Viewer(('127.0.0.1', args.port), args.data_dir) as server:
        print(f'Multipath evidence: http://127.0.0.1:{args.port}/', flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
