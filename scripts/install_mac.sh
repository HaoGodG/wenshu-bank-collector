#!/bin/bash
set -e
cd "$(dirname "$0")/.."
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
echo "安装完成。PyCharm Interpreter 请选择: $(pwd)/.venv/bin/python"
