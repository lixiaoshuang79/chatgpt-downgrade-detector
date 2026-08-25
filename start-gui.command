#!/bin/bash
# ChatGPT 降智检测器 —— macOS 桌面启动器
# 双击运行：启动本地 Web GUI 并自动打开浏览器。
cd "$(dirname "$0")"
exec python3 detector/gui.py
