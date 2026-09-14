#!/usr/bin/env bash
# Запуск Pasport Checker на Linux/macOS
cd "$(dirname "$0")" || exit 1
exec python3 app.py
