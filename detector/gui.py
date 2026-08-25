#!/usr/bin/env python3
"""ChatGPT 降智检测器 —— GUI 入口。

用法:
  python3 gui.py                # 默认端口 8899，自动打开浏览器
  python3 gui.py --port 9000    # 指定端口
  python3 gui.py --no-open      # 不自动打开浏览器
"""
import argparse
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gui_server import start_server  # noqa: E402

BANNER = r"""
  ___ _  _ ___ ___   ___  ___  ___  _  _   ___   _   ___ ___ ___  _  _
 / __| || |_ _|   \ / _ \|   \|   \| || | / __| /_\ | _ \_ _|   \| || |
| (_ | __ | || |) | (_) | |) | |) | __ | \__ \/ _ \|   /| || |) | __ |
 \___|_||_|___|___/ \___/|___/|___/|_||_| |___/_/ \_\_|_\___|___/|_||_|
"""


def main():
    ap = argparse.ArgumentParser(description="ChatGPT 降智检测器 GUI")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    print(BANNER)
    print("ChatGPT 降智检测器 — Web GUI")
    print(f"  地址: http://127.0.0.1:{args.port}")
    print("  Ctrl+C 退出\n")

    srv = start_server(args.port)
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
