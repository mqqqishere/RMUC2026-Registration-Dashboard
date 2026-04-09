#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地部署 RMUC 2026 晋级推演台，并提供刷新接口。"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS_DIR = os.path.join(ROOT, "docs")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import build_dashboard  # noqa: E402


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class):
        super().__init__(server_address, handler_class)
        self.refresh_lock = threading.Lock()


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DOCS_DIR, **kwargs)

    def end_headers(self):
        path = self.path.split("?", 1)[0]
        if path.endswith((".html", ".json", ".csv")) or path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):
        if self.path != "/api/refresh":
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown API path")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length:
            self.rfile.read(length)

        if not self.server.refresh_lock.acquire(blocking=False):
            self._send_json(HTTPStatus.CONFLICT, {
                "error": "已有刷新任务正在执行，请稍后再试。",
            })
            return

        try:
            dashboard = load_build_dashboard_module()
            payload = dashboard.build_payload()
            if payload.get("fetch_error"):
                self._send_json(HTTPStatus.BAD_GATEWAY, {
                    "error": payload["fetch_error"],
                })
                return
            dashboard.write_outputs(payload)
            self._send_json(HTTPStatus.OK, payload)
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": f"{type(exc).__name__}: {exc}",
            })
        finally:
            self.server.refresh_lock.release()

    def _send_json(self, status: HTTPStatus, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def load_build_dashboard_module():
    """Reload dashboard-related modules so refresh picks up local code edits."""
    module_names = ["qingflow", "allocator", "qualification", "build_dashboard"]
    for name in module_names:
        module = sys.modules.get(name)
        if module is not None:
            importlib.reload(module)
    return sys.modules.get("build_dashboard", build_dashboard)


def initial_refresh() -> bool:
    dashboard = load_build_dashboard_module()
    payload = dashboard.build_payload()
    if payload.get("fetch_error"):
        print(
            f"[warn] initial refresh failed: {payload['fetch_error']}",
            file=sys.stderr,
        )
        return False
    dashboard.write_outputs(payload)
    print(
        f"[ok] initial refresh completed: submitted={payload['submission']['submitted_count']} "
        f"updated={payload['updated_at_cst']}"
    )
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve RMUC 2026 晋级推演台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--skip-initial-refresh",
        action="store_true",
        help="skip the initial qingflow refresh on startup",
    )
    args = parser.parse_args(argv)

    if not args.skip_initial_refresh:
        initial_refresh()

    httpd = DashboardHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Serving RMUC dashboard at http://{args.host}:{args.port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
