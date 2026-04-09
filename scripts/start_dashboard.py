#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动 RMUC 本地看板，并自动打开浏览器。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNTIME_DIR = os.path.join(ROOT, ".runtime")
SERVE_SCRIPT = os.path.join(HERE, "serve_dashboard.py")
STARTUP_TIMEOUT = 20.0


def runtime_paths(port: int) -> tuple[str, str]:
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    return (
        os.path.join(RUNTIME_DIR, f"dashboard-{port}.pid"),
        os.path.join(RUNTIME_DIR, f"dashboard-{port}.log"),
    )


def dashboard_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def data_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/data.json"


def write_pidfile(path: str, pid: int):
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(pid))


def read_pidfile(path: str) -> int | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def remove_file(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def local_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def is_dashboard_ready(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(data_url(host, port), timeout=1.2) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("title") == "RMUC 2026 晋级推演台"
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return False


def wait_until_ready(host: str, port: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_dashboard_ready(host, port):
            return True
        time.sleep(0.4)
    return False


def tail_log(path: str, lines: int = 30) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.readlines()
        return "".join(content[-lines:])
    except OSError:
        return ""


def launch_server(host: str, port: int, skip_initial_refresh: bool) -> tuple[int, str]:
    pid_path, log_path = runtime_paths(port)
    log_file = open(log_path, "a", encoding="utf-8")
    cmd = [sys.executable, SERVE_SCRIPT, "--host", host, "--port", str(port)]
    if skip_initial_refresh:
        cmd.append("--skip-initial-refresh")

    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    write_pidfile(pid_path, proc.pid)
    return proc.pid, log_path


def ensure_server(host: str, port: int, skip_initial_refresh: bool) -> tuple[bool, str]:
    pid_path, log_path = runtime_paths(port)
    pid = read_pidfile(pid_path)

    if is_dashboard_ready(host, port):
        return False, log_path

    if pid and not pid_alive(pid):
        remove_file(pid_path)

    if local_port_open(host, port) and not is_dashboard_ready(host, port):
        raise RuntimeError(
            f"{host}:{port} 已被其他进程占用，且不是当前看板服务。"
        )

    started = False
    if not local_port_open(host, port):
        launch_server(host, port, skip_initial_refresh)
        started = True

    if wait_until_ready(host, port, STARTUP_TIMEOUT):
        return started, log_path

    log_tail = tail_log(log_path)
    raise RuntimeError(
        "看板服务启动超时。\n"
        f"日志文件: {log_path}\n"
        f"{log_tail}"
    )


def open_browser(url: str) -> bool:
    try:
        return webbrowser.open(url, new=1, autoraise=True)
    except webbrowser.Error:
        return False


def stop_server(port: int):
    pid_path, _ = runtime_paths(port)
    pid = read_pidfile(pid_path)
    if not pid:
        print(f"No PID file for port {port}.")
        return
    if not pid_alive(pid):
        remove_file(pid_path)
        print(f"Stale PID file removed for port {port}.")
        return
    os.kill(pid, signal.SIGTERM)
    remove_file(pid_path)
    print(f"Stopped dashboard server on port {port} (pid {pid}).")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start the RMUC local dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser tab")
    parser.add_argument(
        "--skip-initial-refresh",
        action="store_true",
        help="start the local server without the initial Qingflow refresh",
    )
    parser.add_argument("--stop", action="store_true", help="stop the dashboard server for this port")
    args = parser.parse_args(argv)

    if args.stop:
        stop_server(args.port)
        return 0

    started, log_path = ensure_server(args.host, args.port, args.skip_initial_refresh)
    url = dashboard_url(args.host, args.port)

    print("Dashboard ready.")
    print(f"URL: {url}")
    print(f"Log: {log_path}")
    print("Server action: " + ("started" if started else "reused existing"))

    if not args.no_open:
        opened = open_browser(url)
        print("Browser: " + ("opened" if opened else "please open the URL manually"))
    else:
        print("Browser: skipped (--no-open)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
