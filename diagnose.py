"""Сбор сведений о странице записи — чтобы понять, как она устроена.

Скрипт открывает сайты в видимом браузере, выбирает услуги и записывает
в файл diagnose.txt всё, что видит: выпадающие списки, их подписи и
варианты, а также запросы, которыми страница подгружает даты.

Запуск: diagnose.bat (Windows) или python3 diagnose.py

Файл diagnose.txt не содержит ни ваших данных, ни паролей — вы вводите
их только на самом сайте, а сюда попадает лишь устройство страницы.
Перед отправкой файл можно открыть блокнотом и посмотреть.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checker.config import load_config
from checker.scraper import _JS_SELECTS, _find_service_select, _open_context, is_placeholder

OUT_PATH = Path(__file__).resolve().parent / "diagnose.txt"
MAX_SERVICES = 3
MAX_BODY = 1500
WAIT_AFTER_SELECT = 9000


class Report:
    """Пишем сразу и в файл, и на экран — чтобы было видно, что идёт работа."""

    def __init__(self, path: Path):
        self.lines: list[str] = []
        self.path = path

    def write(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)

    def save(self) -> None:
        self.path.write_text("\n".join(self.lines), encoding="utf-8")


def describe_selects(report: Report, selects: list[dict], title: str) -> None:
    report.write(f"  {title}: выпадающих списков на странице — {len(selects)}")
    for select in selects:
        report.write(
            f"    [{select['index']}] id={select['id']!r} name={select['name']!r} "
            f"видим={select['visible']} подпись={select['label']!r}"
        )
        options = select["options"]
        report.write(f"        вариантов: {len(options)}")
        for option in options[:25]:
            flag = " (недоступен)" if option["disabled"] else ""
            report.write(f"          value={option['value']!r} text={option['text']!r}{flag}")
        if len(options) > 25:
            report.write(f"          …ещё {len(options) - 25}")


def dump_html(report: Report, page, selects: list[dict]) -> None:
    """Сохранить разметку самих списков — по ней видно реальную структуру формы."""
    for select in selects:
        try:
            html = page.locator("select").nth(select["index"]).evaluate(
                "el => (el.closest('form') || el.parentElement).outerHTML"
            )
        except Exception as exc:
            report.write(f"    не удалось снять разметку списка {select['index']}: {exc}")
            continue
        report.write(f"    --- разметка вокруг списка [{select['index']}] ---")
        report.write("    " + html[:2500].replace("\n", " "))
        break  # одной формы достаточно, остальные лежат в ней же


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Не установлен Playwright.\n"
            "Запустите install.bat (Windows) или ./install.sh, потом повторите.\n"
        )
        try:
            input("Нажмите Enter, чтобы закрыть окно...")
        except EOFError:
            pass
        return 1

    config = load_config()
    report = Report(OUT_PATH)
    report.write(f"Диагностика от {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    report.write(f"Python {sys.version.split()[0]} на {sys.platform}")
    report.write("=" * 70)

    with sync_playwright() as playwright:
        # Тот же профиль, что и у самой программы: пройденная проверка
        # Cloudflare общая, и подтверждать «я не робот» лишний раз не нужно.
        context, attached = _open_context(playwright, config, headless=False)
        page = context.pages[0] if context.pages else context.new_page()

        traffic: list[str] = []

        def on_response(response):
            """Ловим запросы, которыми страница подтягивает даты."""
            request = response.request
            if request.resource_type not in ("xhr", "fetch"):
                return
            note = [f"  {request.method} {response.status} {request.url}"]
            post = request.post_data
            if post:
                note.append(f"    отправлено: {post[:MAX_BODY]}")
            try:
                body = response.text()
                note.append(f"    получено: {body[:MAX_BODY]}")
            except Exception:
                note.append("    получено: (не текст)")
            traffic.append("\n".join(note))

        page.on("response", on_response)

        for number, site in enumerate(config.get("sites", []), start=1):
            url = site["url"]
            report.write("")
            report.write("=" * 70)
            report.write(f"САЙТ {number}: {site.get('name')} — {url}")
            report.write("=" * 70)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(4000)
            except Exception as exc:
                report.write(f"  НЕ ОТКРЫЛСЯ: {type(exc).__name__}: {exc}")
                continue

            title = page.title()
            report.write(f"  заголовок вкладки: {title!r}")
            if any(mark in title.lower() for mark in ("just a moment", "трохи зачекайте")):
                report.write("  ЭТО ЗАГЛУШКА CLOUDFLARE — поставьте галочку в окне браузера")
                print("\n>>> Поставьте галочку «Підтвердьте, що ви людина» в окне браузера.", flush=True)
                for _ in range(60):
                    page.wait_for_timeout(2000)
                    if not any(m in page.title().lower() for m in ("just a moment", "трохи зачекайте")):
                        report.write("  проверка пройдена, продолжаю")
                        page.wait_for_timeout(2000)
                        break
                else:
                    report.write("  проверку так и не прошли")
                    continue
            selects = page.evaluate(_JS_SELECTS)
            describe_selects(report, selects, "ДО выбора услуги")
            dump_html(report, page, selects)

            service_select = _find_service_select(selects)
            if service_select is None:
                report.write("  СПИСОК УСЛУГ НЕ НАЙДЕН — возможно, форма устроена иначе")
                page.screenshot(path=str(OUT_PATH.with_name(f"diagnose-{number}.png")))
                continue

            report.write(f"  список услуг определён как [{service_select['index']}]")
            options = [
                option
                for option in service_select["options"]
                if not option["disabled"] and not is_placeholder(option["text"], option["value"])
            ]

            for option in options[:MAX_SERVICES]:
                report.write("")
                report.write(f"  --- выбираю услугу: {option['text']!r} ---")
                traffic.clear()
                try:
                    locator = page.locator("select").nth(service_select["index"])
                    if option["value"]:
                        locator.select_option(value=option["value"])
                    else:
                        locator.select_option(label=option["text"])
                    page.wait_for_timeout(WAIT_AFTER_SELECT)
                except Exception as exc:
                    report.write(f"    не удалось выбрать: {type(exc).__name__}: {exc}")
                    continue

                after = page.evaluate(_JS_SELECTS)
                describe_selects(report, after, "ПОСЛЕ выбора услуги")

                if traffic:
                    report.write("    запросы, ушедшие после выбора:")
                    for item in traffic:
                        report.write(item)
                else:
                    report.write("    запросов не было — значит, даты уже лежали в странице")

                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(3000)

            page.screenshot(path=str(OUT_PATH.with_name(f"diagnose-{number}.png")))
            report.write(f"  снимок экрана: diagnose-{number}.png")

        if not attached:
            context.close()

    report.write("")
    report.write("=" * 70)
    report.write("Готово.")
    report.save()

    print(f"\nВсё записано в файл:\n  {OUT_PATH}\nПришлите его — по нему я настрою программу.")
    try:
        input("\nНажмите Enter, чтобы закрыть окно...")
    except EOFError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
