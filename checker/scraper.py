"""Чтение доступных дат записи со страницы электронной очереди.

Страница устроена так: сначала выбирается «Послуга», после чего появляется
выпадающий список «Обрати день» с доступными датами. Список дат подгружается
скриптами, поэтому основной режим — настоящий браузер (Playwright/Chromium).
Режим http оставлен как быстрый запасной вариант для страниц, где даты уже
лежат в исходном HTML.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .dates import parse_date

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Сколько услуг максимум перебирать, если в настройках не задана конкретная.
MAX_SERVICES = 8
# Сколько ждать появления списка дат после выбора услуги.
DAYS_WAIT_SECONDS = 25

# Тексты кнопок согласия с cookie, которые могут перекрывать форму.
_CONSENT_TEXTS = ("Прийняти", "Приймаю", "Погоджуюсь", "Зрозуміло", "Accept", "Согласен", "OK")

_PLACEHOLDER_RE = re.compile(r"^[\s\-–—]*(обрати|оберіть|выбрать|выберите|select|choose)?[\s\-–—]*$", re.I)


class ScraperError(RuntimeError):
    """Проверка не удалась — сеть, таймаут или неожиданная разметка."""


@dataclass
class ServiceResult:
    """Даты, найденные для одной услуги."""

    service: str
    dates: list[dt.date] = field(default_factory=list)
    note: str = ""


@dataclass
class SiteResult:
    """Итог проверки одного сайта."""

    name: str
    url: str
    ok: bool = False
    error: str = ""
    mode: str = ""
    services: list[ServiceResult] = field(default_factory=list)
    checked_at: dt.datetime = field(default_factory=dt.datetime.now)

    @property
    def all_dates(self) -> list[dt.date]:
        seen: set[dt.date] = set()
        for service in self.services:
            seen.update(service.dates)
        return sorted(seen)

    @property
    def earliest(self) -> dt.date | None:
        dates = self.all_dates
        return dates[0] if dates else None

    def matches(self, deadline: dt.date) -> list[tuple[str, dt.date]]:
        """Пары (услуга, дата) с датами не позже deadline — то, ради чего всё затевалось."""
        found = []
        for service in self.services:
            for value in service.dates:
                if value <= deadline:
                    found.append((service.service, value))
        return sorted(found, key=lambda pair: pair[1])


def is_placeholder(text: str, value: str = "") -> bool:
    """Пункт «- Обрати -» и прочие пустышки не являются реальным выбором."""
    if value.strip() in ("", "0", "-1", "_none"):
        return True
    return bool(_PLACEHOLDER_RE.match(text or ""))


def check_site(site: dict, config: dict, log=lambda message: None) -> SiteResult:
    """Проверить один сайт в режиме из настроек, с откатом на http при сбое браузера."""
    mode = (config.get("mode") or "browser").lower()
    name = site.get("name") or site.get("url", "")
    url = site["url"]

    if mode == "http":
        return check_site_http(site, config, log)

    try:
        return check_site_browser(site, config, log)
    except ScraperError as exc:
        log(f"[{name}] браузер не справился: {exc}")
        log(f"[{name}] пробую запасной режим http")
        result = check_site_http(site, config, log)
        if not result.ok:
            result.error = f"браузер: {exc}; http: {result.error}"
        elif not result.all_dates:
            result.error = f"браузер: {exc}"
            result.ok = False
        return result


# --------------------------------------------------------------------------
# Режим браузера
# --------------------------------------------------------------------------

# Собирает описание всех <select> на странице: подпись, видимость и варианты.
_JS_SELECTS = r"""
() => {
  const labels = Array.from(document.querySelectorAll('label'));
  const labelFor = (el) => {
    if (el.id) {
      const direct = labels.find(l => l.htmlFor === el.id);
      if (direct) return direct.innerText || direct.textContent || '';
    }
    const wrapper = el.closest('label');
    if (wrapper) return wrapper.innerText || wrapper.textContent || '';
    let node = el.parentElement, depth = 0;
    while (node && depth < 4) {
      const nearby = node.querySelector('label');
      if (nearby) return nearby.innerText || nearby.textContent || '';
      node = node.parentElement;
      depth++;
    }
    return '';
  };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('select')).map((el, index) => ({
    index,
    id: el.id || '',
    name: el.name || '',
    label: clean(labelFor(el)),
    visible: el.getClientRects().length > 0,
    options: Array.from(el.options).map(o => ({
      value: o.value || '',
      text: clean(o.textContent),
      disabled: !!o.disabled
    }))
  }));
}
"""


def check_site_browser(site: dict, config: dict, log=lambda message: None) -> SiteResult:
    """Открыть страницу в Chromium, выбрать услугу и прочитать список дат."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ScraperError(
            "не установлен Playwright. Запустите install.bat (Windows) или "
            "install.sh (macOS/Linux)"
        ) from exc

    name = site.get("name") or site["url"]
    url = site["url"]
    wanted = (site.get("service") or "").strip()
    timeout_ms = int(config.get("timeout_seconds", 60)) * 1000
    headless = bool(config.get("headless", True))
    result = SiteResult(name=name, url=url, mode="browser")

    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, headless, config)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="uk-UA",
                    viewport={"width": 1280, "height": 900},
                )
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                _dismiss_consent(page)
                _wait_for_selects(page, timeout_ms)

                selects = page.evaluate(_JS_SELECTS)
                service_select = _find_service_select(selects)

                if service_select is None:
                    dates = _collect_dates(selects, skip=None)
                    result.services = [ServiceResult("(без выбора услуги)", dates)]
                    result.ok = True
                    log(f"[{name}] список услуг не найден, прочитаны даты со страницы: {len(dates)}")
                    return result

                options = _service_options(service_select, wanted)
                if not options:
                    available = ", ".join(
                        o["text"] for o in service_select["options"] if not is_placeholder(o["text"], o["value"])
                    )
                    raise ScraperError(
                        f"услуга «{wanted}» не найдена. Доступны: {available or 'нет вариантов'}"
                    )

                for position, option in enumerate(options):
                    if position > 0:
                        # Перезагружаем страницу, чтобы форма не тянула состояние прошлой услуги.
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        _dismiss_consent(page)
                        _wait_for_selects(page, timeout_ms)
                        selects = page.evaluate(_JS_SELECTS)
                        service_select = _find_service_select(selects) or service_select

                    dates, note = _pick_service_dates(page, service_select, option, log, name)
                    result.services.append(ServiceResult(option["text"], dates, note))

                result.ok = True
                return result
            finally:
                browser.close()
    except ScraperError:
        raise
    except PlaywrightError as exc:
        raise ScraperError(_short(str(exc))) from exc
    except Exception as exc:  # pragma: no cover - неожиданные сбои окружения
        raise ScraperError(_short(f"{type(exc).__name__}: {exc}")) from exc


def _launch_browser(playwright, headless: bool, config: dict):
    """Запустить Chromium: свой, скачанный Playwright, либо уже стоящий в системе Chrome/Edge.

    Так приложение работает и без отдельной загрузки браузера — если на
    компьютере уже есть Chrome или Edge.
    """
    custom = (config.get("browser_path") or "").strip()
    attempts: list[tuple[str, dict]] = []
    if custom:
        attempts.append((f"браузер из настроек ({custom})", {"executable_path": custom}))
    attempts += [
        ("Chromium от Playwright", {}),
        ("системный Google Chrome", {"channel": "chrome"}),
        ("системный Microsoft Edge", {"channel": "msedge"}),
    ]

    problems = []
    for title, options in attempts:
        try:
            return playwright.chromium.launch(headless=headless, **options)
        except Exception as exc:
            problems.append(f"{title}: {_short(str(exc), 120)}")

    raise ScraperError(
        "не удалось запустить браузер. Установите его командой "
        "«playwright install chromium» (или запустите install.bat / install.sh), "
        "либо укажите путь к Chrome в поле browser_path в config.json. "
        + " | ".join(problems)
    )

def _pick_service_dates(page, service_select, option, log, name) -> tuple[list[dt.date], str]:
    """Выбрать услугу и дождаться, пока подгрузится список дат."""
    locator = page.locator("select").nth(service_select["index"])
    if option["value"]:
        locator.select_option(value=option["value"])
    else:
        locator.select_option(label=option["text"])

    deadline_ts = time.monotonic() + DAYS_WAIT_SECONDS
    dates: list[dt.date] = []
    while time.monotonic() < deadline_ts:
        page.wait_for_timeout(700)
        fresh = page.evaluate(_JS_SELECTS)
        dates = _collect_dates(fresh, skip=service_select)
        if dates:
            break

    note = "" if dates else "свободных дат нет"
    log(f"[{name}] {option['text']}: найдено дат — {len(dates)}")
    return dates, note


def _dismiss_consent(page) -> None:
    """Закрыть баннер cookie, если он перекрывает форму."""
    for text in _CONSENT_TEXTS:
        try:
            button = page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            if button.count() and button.first.is_visible():
                button.first.click(timeout=2000)
                return
        except Exception:
            continue


def _wait_for_selects(page, timeout_ms: int) -> None:
    try:
        page.wait_for_selector("select", timeout=min(timeout_ms, 30000))
    except Exception as exc:
        raise ScraperError("на странице не появилась форма записи") from exc


def _find_service_select(selects: list[dict]) -> dict | None:
    """Найти выпадающий список услуг — по подписи «Послуга» или по содержимому."""
    for select in selects:
        if "послуг" in select["label"].lower():
            return select

    for select in selects:
        joined = " ".join(option["text"].lower() for option in select["options"])
        if any(word in joined for word in ("паспорт", "картка", "карта")):
            return select

    return None


def _service_options(service_select: dict, wanted: str) -> list[dict]:
    """Список услуг для проверки: либо совпавшие с настройкой, либо все подряд."""
    real = [
        option
        for option in service_select["options"]
        if not option["disabled"] and not is_placeholder(option["text"], option["value"])
    ]

    if wanted and wanted != "*":
        needle = wanted.casefold()
        matched = [option for option in real if needle in option["text"].casefold()]
        return matched

    return real[:MAX_SERVICES]


def _collect_dates(selects: list[dict], skip: dict | None) -> list[dt.date]:
    """Собрать все даты из выпадающих списков, кроме списка услуг."""
    found: set[dt.date] = set()
    for select in selects:
        if skip is not None and _same_select(select, skip):
            continue
        for option in select["options"]:
            if option["disabled"]:
                continue
            value = parse_date(option["text"]) or parse_date(option["value"])
            if value:
                found.add(value)
    return sorted(found)


# --------------------------------------------------------------------------
# Запасной режим без браузера
# --------------------------------------------------------------------------

_OPTION_RE = re.compile(r"<option[^>]*>(.*?)</option>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def check_site_http(site: dict, config: dict, log=lambda message: None) -> SiteResult:
    """Забрать HTML и вытащить даты из <option> без запуска браузера."""
    name = site.get("name") or site["url"]
    url = site["url"]
    result = SiteResult(name=name, url=url, mode="http")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "uk-UA,uk;q=0.9",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=int(config.get("timeout_seconds", 60))) as response:
            html = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        result.error = _short(f"{type(exc).__name__}: {exc}")
        log(f"[{name}] http-запрос не удался: {result.error}")
        return result

    dates = sorted({value for value in (parse_date(text) for text in _option_texts(html)) if value})
    result.services = [ServiceResult("(режим http)", dates)]
    result.ok = True
    if not dates:
        result.services[0].note = "даты не найдены в HTML — нужен режим browser"
    log(f"[{name}] режим http: найдено дат — {len(dates)}")
    return result


def _option_texts(html: str) -> list[str]:
    return [_TAG_RE.sub(" ", chunk).strip() for chunk in _OPTION_RE.findall(html)]


def _same_select(candidate: dict, target: dict) -> bool:
    """Узнать тот же самый список.

    После выбора услуги форма может перерисоваться, и порядковый номер съедет,
    поэтому сначала сверяем id и name и только потом — номер.
    """
    for key in ("id", "name"):
        if target.get(key):
            return candidate.get(key) == target[key]
    return candidate["index"] == target["index"]


def _short(message: str, limit: int = 200) -> str:
    """Сообщения Playwright бывают на десятки строк — в лог нужна суть."""
    first = (message or "").strip().splitlines()
    text = first[0] if first else ""
    return text[:limit] + ("…" if len(text) > limit else "")
