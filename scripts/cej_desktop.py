#!/usr/bin/env python3
import argparse
import socket
import threading
from pathlib import Path

try:
    import webview
except ImportError as exc:
    raise SystemExit(
        "pywebview is not installed. Install it with: python3 -m pip install --user pywebview"
    ) from exc

import cej_web_ui


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Desktop launcher for the CEJ dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for the embedded local server")
    parser.add_argument("--port", type=int, default=8765, help="Bind port. Default: 8765")
    parser.add_argument("--width", type=int, default=1440, help="Window width")
    parser.add_argument("--height", type=int, default=980, help="Window height")
    parser.add_argument("--title", default="Tableau de bord CEJ", help="Desktop window title")
    return parser.parse_args()


def pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main() -> int:
    args = parse_args()
    port = args.port or pick_free_port(args.host)

    server = cej_web_ui.ThreadingHTTPServer((args.host, port), cej_web_ui.CejHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="cej-web-ui")
    thread.start()

    try:
        webview.create_window(
            args.title,
            f"http://{args.host}:{port}",
            width=args.width,
            height=args.height,
            min_size=(1100, 760),
            text_select=True,
        )
        webview.start(gui="qt")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
