"""Настройки приложения: чтение, слияние с умолчаниями и сохранение в config.json."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"

# Услуга по умолчанию — подстрока, по которой ищется пункт в списке «Послуга».
DEFAULT_SERVICE = "Закордонний паспорт"

DEFAULT_CONFIG: dict = {
    # Уведомлять, если есть свободная дата НЕ ПОЗЖЕ этой (включительно).
    "deadline": "02.10.2026",
    # Как часто перепроверять сайты, в минутах.
    "interval_minutes": 15,
    # browser — настоящий Chromium через Playwright (надёжно, видит JS);
    # http — быстрый разбор HTML без браузера (работает не на всех страницах).
    "mode": "browser",
    # Прятать окно браузера во время проверки.
    "headless": True,
    # Повторять уведомление про ту же дату не чаще, чем раз в N минут.
    "notify_repeat_minutes": 120,
    # Звуковой сигнал вместе с уведомлением.
    "sound": True,
    # Сколько секунд ждать ответа сайта.
    "timeout_seconds": 60,
    # Путь к своему браузеру. Пусто — искать Chromium от Playwright,
    # затем установленные в системе Chrome и Edge.
    "browser_path": "",
    "sites": [
        {
            "name": "Варшава, Єрусалимські алеї 179",
            "url": "https://warszawa.pasport.org.ua/solutions/e-queue",
            "service": DEFAULT_SERVICE,
            "enabled": True,
        },
        {
            "name": "Гданськ",
            "url": "https://gdansk.pasport.org.ua/solutions/e-queue",
            "service": DEFAULT_SERVICE,
            "enabled": True,
        },
    ],
}


def load_config(path: os.PathLike | str = CONFIG_PATH) -> dict:
    """Прочитать config.json, дополнив недостающие поля значениями по умолчанию."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    path = Path(path)
    if not path.exists():
        return config

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Битый конфиг не должен мешать запуску — работаем на умолчаниях.
        return config

    if not isinstance(raw, dict):
        return config

    for key, value in raw.items():
        if key == "sites":
            config["sites"] = _normalize_sites(value)
        elif key in config:
            config[key] = value

    return config


def _normalize_sites(value) -> list[dict]:
    if not isinstance(value, list):
        return copy.deepcopy(DEFAULT_CONFIG["sites"])

    sites = []
    for item in value:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        sites.append(
            {
                "name": str(item.get("name") or item["url"]),
                "url": str(item["url"]),
                "service": str(item.get("service", DEFAULT_SERVICE)),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return sites or copy.deepcopy(DEFAULT_CONFIG["sites"])


def save_config(config: dict, path: os.PathLike | str = CONFIG_PATH) -> None:
    """Сохранить настройки, не теряя старый файл при сбое записи."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def enabled_sites(config: dict) -> list[dict]:
    return [site for site in config.get("sites", []) if site.get("enabled", True)]
