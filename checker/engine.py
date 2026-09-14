"""Логика проверки: обход сайтов, отбор подходящих дат и уведомления без спама."""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field

from .config import enabled_sites
from .dates import fmt_date, parse_deadline
from .notify import notify
from .scraper import SiteResult, check_site


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


def run_check(config: dict, log=lambda message: None, notifier: Notifier | None = None) -> CheckRound:
    """Обойти все включённые сайты и вернуть результат круга проверки."""
    deadline = parse_deadline(config.get("deadline", "02.10.2026"))
    round_result = CheckRound()
    sites = enabled_sites(config)

    if not sites:
        log("В настройках нет ни одного включённого сайта.")
        return round_result

    log(f"Проверяю сайты ({len(sites)} шт.), ищу даты не позже {fmt_date(deadline)}")

    for site in sites:
        name = site.get("name") or site["url"]
        try:
            result = check_site(site, config, log)
        except Exception as exc:  # проверка одного сайта не должна ронять остальные
            result = SiteResult(name=name, url=site["url"], ok=False, error=f"{type(exc).__name__}: {exc}")

        round_result.results.append(result)

        if not result.ok:
            log(f"[{name}] ОШИБКА: {result.error}")
            continue

        for service, date_value in result.matches(deadline):
            round_result.matches.append(Match(name, service, date_value, site["url"]))

        earliest = result.earliest
        log(f"[{name}] ближайшая дата: {fmt_date(earliest)} (всего дат: {len(result.all_dates)})")

    if notifier is not None and round_result.matches:
        round_result.new_matches = notifier.filter_new(round_result.matches)
        if round_result.new_matches:
            send_notification(round_result.new_matches, config, log)

    if round_result.matches:
        log(f"НАЙДЕНО подходящих дат: {len(round_result.matches)}")
    else:
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


class Monitor(threading.Thread):
    """Фоновый поток: проверяет сайты по кругу с заданным интервалом."""

    def __init__(self, config: dict, on_event, lock: threading.Lock | None = None,
                 notifier: Notifier | None = None):
        super().__init__(daemon=True)
        self.config = config
        self.on_event = on_event
        self.lock = lock or threading.Lock()
        self.notifier = notifier or Notifier(int(config.get("notify_repeat_minutes", 120)))
        self._stop = threading.Event()
        self._wake = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            self._run_once()
            if self._stop.is_set():
                break
            interval = max(1, int(self.config.get("interval_minutes", 15))) * 60
            self.on_event("next", interval)
            self._wake.wait(interval)
            self._wake.clear()

    def _run_once(self) -> None:
        with self.lock:
            self.on_event("started", None)
            try:
                round_result = run_check(self.config, lambda message: self.on_event("log", message), self.notifier)
                self.on_event("finished", round_result)
            except Exception as exc:
                self.on_event("log", f"Проверка прервана: {type(exc).__name__}: {exc}")
                self.on_event("finished", CheckRound())

    def check_now(self) -> None:
        """Не ждать конца интервала — проверить прямо сейчас."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
