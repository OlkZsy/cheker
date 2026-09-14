"""Чтение доступных дат записи со страницы электронной очереди.

Страница устроена так: выбирается «Послуга», после чего скрипты запрашивают
у сервера список дней и заполняют список «Обрати день» (дальше идёт ещё выбор
часа, но он нам не нужен). Поэтому читать даты можно только в настоящем
браузере — в исходном HTML их нет.

Сайт закрыт защитой Cloudflare, которая иногда просит подтвердить, что вы не
робот. Программа не пытается эту проверку обходить: она открывает видимое окно
браузера, ждёт, пока галочку поставит человек, и дальше работает в этой же
сессии. Профиль браузера сохраняется на диск, поэтому подтверждение живёт
между проверками и перезапусками.
"""

from __future__ import annotations

import datetime as dt
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .dates import parse_date

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = APP_DIR / "browser-profile"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# Сколько услуг максимум перебирать, если в настройках не задана конкретная.
MAX_SERVICES = 8
# Сколько ждать появления списка дат после выбора услуги.
DAYS_WAIT_SECONDS = 25

_CONSENT_TEXTS = ("Прийняти", "Приймаю", "Погоджуюсь", "Зрозуміло", "Accept", "Согласен", "OK")

# Признаки страницы-заглушки Cloudflare, а не самой формы записи.
_CHALLENGE_TITLES = ("just a moment", "трохи зачекайте", "attention required", "один момент")
_CHALLENGE_TEXTS = (
    "триває перевірка безпеки",
    "підтвердьте, що ви людина",
    "verify you are human",
    "checking your browser",
    "перевіряє, що ви не бот",
)
_CHALLENGE_SELECTORS = "#challenge-form, #challenge-running, #cf-challenge-running"

# Сообщение сайта, когда свободных мест нет вовсе.
_NO_SLOTS_RE = re.compile(r"відсутн\w*\s+місц", re.I)

_PLACEHOLDER_RE = re.compile(r"^[\s\-–—]*(обрати|оберіть|выбрать|выберите|select|choose)?[\s\-–—]*$", re.I)


class ScraperError(RuntimeError):
    """Проверка не удалась — сеть, таймаут или неожиданная разметка."""


class ChallengeError(ScraperError):
    """Cloudflare просит подтвердить, что за компьютером человек."""


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
    needs_human: bool = False
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


class BrowserSession:
    """Одно окно браузера на всё время работы программы.

    Постоянное окно решает сразу две задачи: пройденная человеком проверка
    Cloudflare не теряется между проверками, и сайт не дёргается лишними
    запусками браузера.
    """

    def __init__(self, config: dict, log=lambda message: None, should_stop=lambda: False):
        self.config = config
        self.log = log
        # Позволяет оборвать долгое ожидание, когда пользователь закрывает окно.
        self.should_stop = should_stop
        self._playwright = None
        self._context = None
        self._page = None
        self._headless: bool | None = None

    # -- жизненный цикл -------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._context is not None

    def ensure_open(self) -> None:
        """Открыть окно, а если в настройках сменился режим — переоткрыть."""
        wanted = bool(self.config.get("headless", False))
        if self._context is not None and wanted != self._headless:
            self.log("Режим окна браузера изменился — перезапускаю браузер")
            self.close()
        if self._context is None:
            self._start(wanted)

    def _start(self, headless: bool) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ScraperError(
                "не установлен Playwright. Запустите install.bat (Windows) "
                "или install.sh (macOS/Linux)"
            ) from exc

        self._playwright = sync_playwright().start()
        try:
            self._context = _open_context(self._playwright, self.config, headless)
        except Exception:
            self._stop_playwright()
            raise

        self._headless = headless
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._page.set_default_timeout(int(self.config.get("timeout_seconds", 60)) * 1000)

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        self._context = None
        self._page = None
        self._headless = None
        self._stop_playwright()

    def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None

    # -- проверка сайта -------------------------------------------------
    def check(self, site: dict) -> SiteResult:
        """Открыть страницу, перебрать услуги и собрать даты."""
        name = site.get("name") or site["url"]
        url = site["url"]
        wanted = (site.get("service") or "").strip()
        result = SiteResult(name=name, url=url, mode="browser")

        self.ensure_open()
        timeout_ms = int(self.config.get("timeout_seconds", 60)) * 1000

        self._open_page(url, name, timeout_ms)
        selects = self._page.evaluate(_JS_SELECTS)
        service_select = _find_service_select(selects)

        if service_select is None:
            if _NO_SLOTS_RE.search(self._page_text()):
                result.services = [ServiceResult("(услуги не предлагаются)", [], "сайт пишет, что мест нет")]
                result.ok = True
                self.log(f"[{name}] сайт пишет, что свободных мест нет")
                return result
            dates = _collect_dates(selects, skip=None)
            result.services = [ServiceResult("(без выбора услуги)", dates)]
            result.ok = True
            self.log(f"[{name}] список услуг не найден, прочитаны даты со страницы: {len(dates)}")
            return result

        options = _service_options(service_select, wanted)
        if not options:
            available = ", ".join(
                option["text"]
                for option in service_select["options"]
                if not is_placeholder(option["text"], option["value"])
            )
            raise ScraperError(f"услуга «{wanted}» не найдена. Доступны: {available or 'нет вариантов'}")

        for position, option in enumerate(options):
            if position > 0:
                # Перезагружаем страницу, чтобы форма не тянула состояние прошлой услуги.
                self._open_page(url, name, timeout_ms)
                selects = self._page.evaluate(_JS_SELECTS)
                service_select = _find_service_select(selects) or service_select

            dates, note = self._pick_service_dates(service_select, option, name)
            result.services.append(ServiceResult(option["text"], dates, note))

        result.ok = True
        return result

    def _pick_service_dates(self, service_select: dict, option: dict, name: str) -> tuple[list[dt.date], str]:
        """Выбрать услугу и дождаться, пока подгрузится список дат."""
        locator = self._page.locator("select").nth(service_select["index"])
        if option["value"]:
            locator.select_option(value=option["value"])
        else:
            locator.select_option(label=option["text"])

        deadline_ts = time.monotonic() + DAYS_WAIT_SECONDS
        dates: list[dt.date] = []
        note = ""

        while time.monotonic() < deadline_ts:
            if self.should_stop():
                raise ScraperError("проверка прервана")
            self._page.wait_for_timeout(700)

            if self._challenge_visible():
                self._wait_for_human(name)
                note = "после проверки Cloudflare страница перезагружена"
                break

            fresh = self._page.evaluate(_JS_SELECTS)
            dates = _collect_dates(fresh, skip=service_select)
            if dates:
                break

            # Сайт прямо пишет, когда мест нет, — не ждём молча весь таймаут.
            if _NO_SLOTS_RE.search(self._page_text()):
                note = "сайт пишет, что свободных мест нет"
                break

        if not dates and not note:
            note = "свободных дат нет"

        self.log(f"[{name}] {option['text']}: найдено дат — {len(dates)}" + (f" ({note})" if note else ""))
        return dates, note

    # -- вспомогательное ------------------------------------------------
    def _open_page(self, url: str, name: str, timeout_ms: int) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        self._page.wait_for_timeout(1500)
        if self._challenge_visible():
            self._wait_for_human(name)
        _dismiss_consent(self._page)
        try:
            self._page.wait_for_selector("select", timeout=min(timeout_ms, 30000))
        except Exception as exc:
            if self._challenge_visible():
                self._wait_for_human(name)
                return
            raise ScraperError("на странице не появилась форма записи") from exc

    def _page_text(self) -> str:
        try:
            return self._page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            return ""

    def _challenge_visible(self) -> bool:
        """Отличить заглушку Cloudflare от настоящей страницы записи."""
        try:
            title = (self._page.title() or "").lower()
        except Exception:
            return False
        if any(marker in title for marker in _CHALLENGE_TITLES):
            return True
        try:
            if self._page.locator(_CHALLENGE_SELECTORS).count():
                return True
        except Exception:
            pass
        text = self._page_text().lower()
        return any(marker in text for marker in _CHALLENGE_TEXTS)

    def _wait_for_human(self, name: str) -> None:
        """Дождаться, пока проверку «я не робот» пройдёт человек.

        Программа сознательно ничего не обходит: она лишь ждёт, пока галочку
        поставят руками, и продолжает в уже подтверждённой сессии.
        """
        if self._headless:
            raise ChallengeError(
                "сайт просит подтвердить, что вы не робот. Снимите галочку "
                "«Скрывать окно браузера» и нажмите «Проверить сейчас» — "
                "в открывшемся окне поставьте галочку в проверке Cloudflare"
            )

        seconds = int(self.config.get("challenge_wait_seconds", 180))
        self.log(
            f"[{name}] сайт просит подтвердить, что вы не робот. "
            f"Поставьте галочку в открытом окне браузера — жду до {seconds} с."
        )
        try:
            self._page.bring_to_front()
        except Exception:
            pass

        deadline_ts = time.monotonic() + seconds
        while time.monotonic() < deadline_ts:
            if self.should_stop():
                raise ScraperError("ожидание проверки прервано")
            self._page.wait_for_timeout(1000)
            if not self._challenge_visible():
                self.log(f"[{name}] проверка пройдена, продолжаю")
                self._page.wait_for_timeout(1500)
                return

        raise ChallengeError(
            f"проверку «я не робот» не прошли за {seconds} с. "
            "Откройте окно браузера, поставьте галочку и повторите проверку"
        )


def _open_context(playwright, config: dict, headless: bool):
    """Открыть браузер с постоянным профилем.

    Профиль на диске хранит cookie пройденной проверки Cloudflare, поэтому
    подтверждать «я не робот» приходится редко, а не каждые 15 минут.
    """
    profile = Path(config.get("profile_dir") or DEFAULT_PROFILE_DIR)
    profile.mkdir(parents=True, exist_ok=True)

    options = {
        "headless": headless,
        "locale": "uk-UA",
        "viewport": {"width": 1280, "height": 900},
    }

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
    for title, extra in attempts:
        try:
            return playwright.chromium.launch_persistent_context(str(profile), **options, **extra)
        except Exception as exc:
            problems.append(f"{title}: {_short(str(exc), 120)}")

    raise ScraperError(
        "не удалось запустить браузер. Установите его командой "
        "«playwright install chromium» (или запустите install.bat / install.sh), "
        "либо укажите путь к Chrome в поле browser_path в config.json. "
        + " | ".join(problems)
    )


def check_site(site: dict, config: dict, log=lambda message: None, session: BrowserSession | None = None) -> SiteResult:
    """Проверить один сайт в режиме из настроек, с откатом на http при сбое браузера."""
    mode = (config.get("mode") or "browser").lower()
    name = site.get("name") or site.get("url", "")

    if mode == "http":
        return check_site_http(site, config, log)

    owned = session is None
    session = session or BrowserSession(config, log)
    try:
        return session.check(site)
    except ChallengeError as exc:
        # Тут запасной http-режим бесполезен: Cloudflare не пустит и его.
        log(f"[{name}] {exc}")
        return SiteResult(name=name, url=site["url"], ok=False, mode="browser",
                          needs_human=True, error=str(exc))
    except ScraperError as exc:
        if session.should_stop():
            return SiteResult(name=name, url=site["url"], ok=False, mode="browser", error=str(exc))
        log(f"[{name}] браузер не справился: {exc}")
        log(f"[{name}] пробую запасной режим http")
        result = check_site_http(site, config, log)
        if not result.ok:
            result.error = f"браузер: {exc}; http: {result.error}"
        elif not result.all_dates:
            result.error = f"браузер: {exc}"
            result.ok = False
        return result
    except Exception as exc:  # неожиданные сбои окружения
        log(f"[{name}] сбой браузера: {type(exc).__name__}: {_short(str(exc))}")
        return SiteResult(name=name, url=site["url"], ok=False, mode="browser",
                          error=_short(f"{type(exc).__name__}: {exc}"))
    finally:
        if owned:
            session.close()


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
        return [option for option in real if needle in option["text"].casefold()]

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


def _same_select(candidate: dict, target: dict) -> bool:
    """Узнать тот же самый список.

    После выбора услуги форма может перерисоваться, и порядковый номер съедет,
    поэтому сначала сверяем id и name и только потом — номер.
    """
    for key in ("id", "name"):
        if target.get(key):
            return candidate.get(key) == target[key]
    return candidate["index"] == target["index"]


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
        headers={"User-Agent": USER_AGENT, "Accept-Language": "uk-UA,uk;q=0.9"},
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


def _short(message: str, limit: int = 200) -> str:
    """Сообщения Playwright бывают на десятки строк — в лог нужна суть."""
    lines = (message or "").strip().splitlines()
    text = lines[0] if lines else ""
    return text[:limit] + ("…" if len(text) > limit else "")
