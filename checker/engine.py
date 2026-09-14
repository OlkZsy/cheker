"""Логика проверки: обход сайтов, отбор подходящих дат и уведомления без спама."""

from __future__ import annotations

import datetime as dt
import queue
import threading
from dataclasses import dataclass, field

from .config import enabled_sites
from .dates import fmt_date, parse_deadline
from .notify import notify
from .scraper import BrowserSession, SiteResult, check_site


@dataclass
class Match:
    """Свободная дата, попадающая в нужный срок."""

    site: str
    service: str
    date: dt.date
    url: str = ""

    @property
    def key(self) -> str:
        return f"{self.site}|{self.service}|{self.date.isoformat()}"


@dataclass
class CheckRound:
    """Итог одного круга проверки всех сайтов."""

    results: list[SiteResult] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    new_matches: list[Match] = field(default_factory=list)
    started_at: dt.datetime = field(default_factory=dt.datetime.now)

    @property
    def has_errors(self) -> bool:
        return any(not result.ok for result in self.results)

    @property
    def needs_human(self) -> bool:
        """Хотя бы один сайт ждёт, что проверку «я не робот» пройдёт человек."""
        return any(result.needs_human for result in self.results)


class Notifier:
    """Следит, чтобы об одной и той же дате не сообщать каждые пять минут."""

    def __init__(self, repeat_minutes: int = 120):
        self.repeat_minutes = repeat_minutes
        self._last_sent: dict[str, dt.datetime] = {}

    def filter_new(self, matches: list[Match], now: dt.datetime | None = None) -> list[Match]:
        """Оставить только те даты, о которых ещё не сообщали (или сообщали давно)."""
        now = now or dt.datetime.now()
        cooldown = dt.timedelta(minutes=max(0, self.repeat_minutes))
        fresh = []
        for match in matches:
            previous = self._last_sent.get(match.key)
            if previous is None or now - previous >= cooldown:
                fresh.append(match)
                self._last_sent[match.key] = now
        return fresh

    def forget(self) -> None:
        self._last_sent.clear()


def run_check(
    config: dict,
    log=lambda message: None,
    notifier: Notifier | None = None,
    session: BrowserSession | None = None,
    should_stop=lambda: False,
) -> CheckRound:
    """Обойти все включённые сайты и вернуть результат круга проверки."""
    deadline = parse_deadline(config.get("deadline", "02.10.2026"))
    round_result = CheckRound()
    sites = enabled_sites(config)

    if not sites:
        log("В настройках нет ни одного включённого сайта.")
        return round_result

    log(f"Проверяю сайты ({len(sites)} шт.), ищу даты не позже {fmt_date(deadline)}")

    for site in sites:
        if should_stop():
            log("Проверка прервана.")
            break
        name = site.get("name") or site["url"]
        try:
            result = check_site(site, config, log, session)
        except Exception as exc:  # проверка одного сайта не должна ронять остальные
            result = SiteResult(name=name, url=site["url"], ok=False, error=f"{type(exc).__name__}: {exc}")

        round_result.results.append(result)

        if not result.ok:
            if not result.needs_human:
                log(f"[{name}] ОШИБКА: {result.error}")
            continue

        for service, date_value in result.matches(deadline):
            round_result.matches.append(Match(name, service, date_value, site["url"]))

        log(f"[{name}] ближайшая дата: {fmt_date(result.earliest)} (всего дат: {len(result.all_dates)})")

    if notifier is not None and round_result.matches:
        round_result.new_matches = notifier.filter_new(round_result.matches)
        if round_result.new_matches:
            send_notification(round_result.new_matches, config, log)

    if round_result.matches:
        log(f"НАЙДЕНО подходящих дат: {len(round_result.matches)}")
    elif not round_result.needs_human:
        log("Подходящих дат пока нет.")

    return round_result


def send_notification(matches: list[Match], config: dict, log=lambda message: None) -> None:
    """Собрать короткий текст и отправить уведомление на компьютер."""
    title = "Есть запись!" if len(matches) == 1 else f"Есть запись! Свободных дат: {len(matches)}"
    lines = [f"{match.site}: {fmt_date(match.date)} — {match.service}" for match in matches[:5]]
    if len(matches) > 5:
        lines.append(f"…и ещё {len(matches) - 5}")
    message = "\n".join(lines)

    delivered = notify(title, message, sound=bool(config.get("sound", True)))
    log(("Уведомление отправлено: " if delivered else "Уведомление показать не удалось: ") + " / ".join(lines))


class Worker(threading.Thread):
    """Единственный поток, который ходит в браузер.

    Все проверки — и разовые, и по расписанию — идут через него: Playwright
    привязан к потоку, в котором создан, а одно постоянное окно браузера
    хранит пройденную проверку Cloudflare между проверками.
    """

    def __init__(self, config: dict, on_event):
        super().__init__(daemon=True)
        self.config = config
        self.on_event = on_event
        self.notifier = Notifier(int(config.get("notify_repeat_minutes", 120)))
        self.monitoring = False
        self._commands: queue.Queue = queue.Queue()
        self._busy = threading.Event()
        self._shutting_down = False
        self._session: BrowserSession | None = None

    # -- команды снаружи ------------------------------------------------
    def check_now(self) -> None:
        """Проверить прямо сейчас, не дожидаясь конца интервала."""
        self._commands.put("check")

    def start_monitoring(self) -> None:
        self.monitoring = True
        self._commands.put("check")

    def stop_monitoring(self) -> None:
        self.monitoring = False
        self._commands.put("reschedule")

    def shutdown(self) -> None:
        self.monitoring = False
        # Флаг обрывает долгое ожидание внутри проверки, чтобы окно браузера
        # закрылось сразу, а не через три минуты.
        self._shutting_down = True
        self._commands.put("stop")

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    # -- сам поток ------------------------------------------------------
    def run(self) -> None:
        try:
            while True:
                timeout = self._interval_seconds() if self.monitoring else None
                try:
                    command = self._commands.get(timeout=timeout)
                except queue.Empty:
                    command = "check"  # истёк интервал слежения

                if command == "stop":
                    break
                if command == "reschedule":
                    continue
                if command == "check":
                    self._run_once()
                    if self.monitoring:
                        self.on_event("next", self._interval_seconds())
        finally:
            self._close_session()

    def _run_once(self) -> None:
        self._busy.set()
        self.on_event("started", None)
        try:
            round_result = run_check(
                self.config,
                lambda message: self.on_event("log", message),
                self.notifier,
                self._ensure_session(),
                should_stop=lambda: self._shutting_down,
            )
            self.on_event("finished", round_result)
        except Exception as exc:
            self.on_event("log", f"Проверка прервана: {type(exc).__name__}: {exc}")
            self._close_session()  # окно могло остаться в неизвестном состоянии
            self.on_event("finished", None)
        finally:
            self._busy.clear()

    def _ensure_session(self) -> BrowserSession | None:
        if (self.config.get("mode") or "browser").lower() == "http":
            self._close_session()
            return None
        if self._session is None:
            self._session = BrowserSession(
                self.config,
                lambda message: self.on_event("log", message),
                should_stop=lambda: self._shutting_down,
            )
        return self._session

    def _close_session(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _interval_seconds(self) -> int:
        return max(1, int(self.config.get("interval_minutes", 15))) * 60
