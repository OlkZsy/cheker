"""Разбор и форматирование дат в том виде, в котором их отдают сайты."""

from __future__ import annotations

import datetime as dt
import re

# 07.10.2026, 7/10/2026, 07-10-2026
_DMY_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")
# 2026-10-07
_ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")


def parse_date(text: str | None) -> dt.date | None:
    """Вытащить первую дату из строки. Возвращает None, если даты нет."""
    if not text:
        return None

    m = _ISO_RE.search(text)
    if m:
        year, month, day = (int(g) for g in m.groups())
        return _safe_date(year, month, day)

    m = _DMY_RE.search(text)
    if m:
        day, month, year = (int(g) for g in m.groups())
        return _safe_date(year, month, day)

    return None


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def fmt_date(value: dt.date | None) -> str:
    """Дата в привычном для сайта виде дд.мм.гггг."""
    return value.strftime("%d.%m.%Y") if value else "—"


def parse_deadline(text: str) -> dt.date:
    """Разобрать крайнюю дату из настроек. Бросает ValueError при мусоре."""
    value = parse_date((text or "").strip())
    if value is None:
        raise ValueError(f"Не удалось разобрать дату: {text!r}. Ожидается формат дд.мм.гггг")
    return value
