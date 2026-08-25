#!/bin/bash
# ChatGPT 降智检测器 —— macOS 桌面启动器
# 双击运行（或在终端执行）。需要 Python 3 + tkinter（python.org 安装版自带）。
cd "$(dirname "$0")"
exec python3 detector/gui.py
