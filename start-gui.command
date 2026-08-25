#!/bin/bash
# ChatGPT 降智检测器 —— macOS 桌面启动器
# 双击运行：自动探测带依赖的 Python，启动本地 Web GUI 并打开浏览器。
cd "$(dirname "$0")"

# 探测带 websocket-client 依赖的 Python（优先 python.org 安装版）
PY=""
for c in /Library/Frameworks/Python.framework/Versions/*/bin/python3 "$(command -v python3)"; do
  if [ -x "$c" ] && "$c" -c "import websocket, yaml" >/dev/null 2>&1; then
    PY="$c"; break
  fi
done

if [ -z "$PY" ]; then
  echo "未找到带依赖的 Python。"
  echo "请先安装依赖：pip install -r requirements.txt"
  read -r -p "按回车退出…"
  exit 1
fi

exec "$PY" detector/gui.py
