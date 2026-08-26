#!/bin/sh
# Double-clickable launcher for the Serial IAP GUI (macOS).
#
# Runs from the project root so that "host.gui" resolves as a package module,
# and pins the interpreter to host/.venv: the ESP-IDF v5.5.5 Python is built
# without _tkinter and cannot start this GUI.

set -e
cd "$(dirname "$0")/.."

PYTHON="host/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "找不到 $PWD/$PYTHON"
    echo
    echo "请先创建上位机虚拟环境（在项目根目录下执行）："
    echo "    python3 -m venv host/.venv"
    echo "    host/.venv/bin/python -m pip install -r host/requirements.txt"
    echo
    printf "按回车关闭…"
    read -r _
    exit 1
fi

exec "$PYTHON" -B -m host.gui
