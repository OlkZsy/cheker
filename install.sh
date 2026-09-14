#!/usr/bin/env bash
# Разовая установка зависимостей для Linux/macOS
set -e
cd "$(dirname "$0")" || exit 1

echo "Ставлю Playwright и браузер Chromium..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium

echo
echo "Готово. Запускайте программу: ./run.sh (или run.command на macOS)"
