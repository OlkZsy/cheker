"""Pasport Checker — окно программы.

Запуск: двойной клик по run.bat (Windows), run.command (macOS) или run.sh (Linux).
Программа периодически открывает страницы электронной очереди, смотрит доступные
даты записи и сообщает, если появилась дата не позже указанной.
"""

from __future__ import annotations

import datetime as dt
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # Python без tcl/tk — окно построить не на чем
    print(
        "Не найден модуль tkinter, без него окно программы не открыть.\n\n"
        "Windows: переустановите Python с сайта python.org, не снимая\n"
        "         галочку «tcl/tk and IDLE» в списке компонентов.\n"
        "Linux:   sudo apt install python3-tk\n"
        "macOS:   установите Python с python.org (в системном его нет)\n"
    )
    try:
        input("Нажмите Enter, чтобы закрыть окно...")
    except EOFError:
        pass
    raise SystemExit(1) from None

from checker.config import CONFIG_PATH, load_config, save_config
from checker.dates import fmt_date, parse_deadline
from checker.engine import Monitor, Notifier, run_check
from checker.notify import SYSTEM

APP_TITLE = "Pasport Checker — запись в электронную очередь"
LOG_PATH = Path(__file__).resolve().parent / "checker.log"
MAX_LOG_LINES = 600

COLOR_IDLE = "#e8eaed"
COLOR_BUSY = "#d6e4ff"
COLOR_HIT = "#b7f0c2"
COLOR_MISS = "#f1f3f4"
COLOR_ERROR = "#ffd9c7"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("940x660")
        self.minsize(780, 560)

        self.config_data = load_config()
        self.events: queue.Queue = queue.Queue()
        self.check_lock = threading.Lock()
        self.notifier = Notifier(int(self.config_data.get("notify_repeat_minutes", 120)))
        self.monitor: Monitor | None = None
        self.next_check_at: dt.datetime | None = None
        self.row_sites: dict[str, dict] = {}

        self._build_ui()
        self._load_settings_into_widgets()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self._drain_events)
        self.after(1000, self._tick_countdown)
        self.log(f"Готово к работе. Настройки: {CONFIG_PATH}")
        self.log("Нажмите «Проверить сейчас» для разовой проверки или «Старт» для слежения.")

    # ------------------------------------------------------------------
    # Интерфейс
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        padding = {"padx": 6, "pady": 4}

        settings = ttk.LabelFrame(self, text="Настройки")
        settings.pack(fill="x", padx=10, pady=(10, 6))

        ttk.Label(settings, text="Нужна дата не позже (включительно):").grid(row=0, column=0, sticky="w", **padding)
        self.deadline_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.deadline_var, width=14).grid(row=0, column=1, sticky="w", **padding)

        ttk.Label(settings, text="Проверять каждые (минут):").grid(row=0, column=2, sticky="w", **padding)
        self.interval_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.interval_var, width=8).grid(row=0, column=3, sticky="w", **padding)

        self.headless_var = tk.BooleanVar()
        ttk.Checkbutton(settings, text="Скрывать окно браузера", variable=self.headless_var).grid(
            row=1, column=0, sticky="w", **padding
        )
        self.sound_var = tk.BooleanVar()
        ttk.Checkbutton(settings, text="Звук при находке", variable=self.sound_var).grid(
            row=1, column=1, sticky="w", **padding
        )
        ttk.Label(settings, text="Режим:").grid(row=1, column=2, sticky="e", **padding)
        self.mode_var = tk.StringVar()
        ttk.Combobox(
            settings, textvariable=self.mode_var, values=["browser", "http"], width=10, state="readonly"
        ).grid(row=1, column=3, sticky="w", **padding)

        main_buttons = ttk.Frame(self)
        main_buttons.pack(fill="x", padx=10)
        self.start_button = ttk.Button(main_buttons, text="Старт", command=self.toggle_monitor)
        self.start_button.pack(side="left", padx=(0, 6))
        self.check_button = ttk.Button(main_buttons, text="Проверить сейчас", command=self.check_now)
        self.check_button.pack(side="left", padx=6)
        ttk.Button(main_buttons, text="Сохранить настройки", command=self.save_settings).pack(side="left", padx=6)
        ttk.Button(main_buttons, text="Открыть сайт", command=self.open_selected_site).pack(side="left", padx=6)

        extra_buttons = ttk.Frame(self)
        extra_buttons.pack(fill="x", padx=10, pady=(6, 0))
        ttk.Button(extra_buttons, text="Вкл/выкл сайт", command=self.toggle_selected_site).pack(side="left", padx=(0, 6))
        ttk.Button(extra_buttons, text="Открыть config.json", command=self.open_config_file).pack(side="left", padx=6)
        ttk.Button(extra_buttons, text="Установить браузер", command=self.install_browser).pack(side="left", padx=6)

        self.banner = tk.Label(
            self,
            text="Ожидание",
            font=("Segoe UI", 14, "bold"),
            bg=COLOR_IDLE,
            pady=12,
            justify="center",
            wraplength=880,
        )
        self.banner.pack(fill="x", padx=10, pady=8)
        self.bind("<Configure>", self._on_resize)

        columns = ("site", "status", "earliest", "hits", "checked")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=7)
        for column, title, width in (
            ("site", "Сайт", 300),
            ("status", "Состояние", 230),
            ("earliest", "Ближайшая дата", 130),
            ("hits", "Подходящих", 100),
            ("checked", "Проверено", 110),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor="w")
        self.tree.tag_configure("hit", background=COLOR_HIT)
        self.tree.tag_configure("error", background=COLOR_ERROR)
        self.tree.tag_configure("off", foreground="#888888")
        self.tree.pack(fill="x", padx=10)

        log_frame = ttk.LabelFrame(self, text="Журнал")
        log_frame.pack(fill="both", expand=True, padx=10, pady=8)
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="Остановлено")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=12, pady=(0, 8))

        self._refresh_table()

    def _load_settings_into_widgets(self) -> None:
        self.deadline_var.set(str(self.config_data.get("deadline", "02.10.2026")))
        self.interval_var.set(str(self.config_data.get("interval_minutes", 15)))
        self.headless_var.set(bool(self.config_data.get("headless", True)))
        self.sound_var.set(bool(self.config_data.get("sound", True)))
        self.mode_var.set(str(self.config_data.get("mode", "browser")))

    def _collect_settings(self) -> bool:
        """Перенести значения из полей в настройки. False — если введена ерунда."""
        try:
            deadline = parse_deadline(self.deadline_var.get())
        except ValueError as exc:
            messagebox.showerror("Неверная дата", str(exc))
            return False

        try:
            interval = int(str(self.interval_var.get()).strip())
            if interval < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Неверный интервал", "Интервал — целое число минут, не меньше 1.")
            return False

        self.config_data["deadline"] = fmt_date(deadline)
        self.deadline_var.set(fmt_date(deadline))
        self.config_data["interval_minutes"] = interval
        self.config_data["headless"] = bool(self.headless_var.get())
        self.config_data["sound"] = bool(self.sound_var.get())
        self.config_data["mode"] = self.mode_var.get() or "browser"
        return True

    # ------------------------------------------------------------------
    # Действия пользователя
    # ------------------------------------------------------------------
    def save_settings(self) -> None:
        if not self._collect_settings():
            return
        try:
            save_config(self.config_data)
            self.log(f"Настройки сохранены в {CONFIG_PATH}")
        except OSError as exc:
            messagebox.showerror("Не удалось сохранить", str(exc))

    def toggle_monitor(self) -> None:
        if self.monitor and self.monitor.is_alive():
            self.monitor.stop()
            self.monitor = None
            self.next_check_at = None
            self.start_button.configure(text="Старт")
            self.status_var.set("Остановлено")
            self.log("Слежение остановлено.")
            return

        if not self._collect_settings():
            return
        self.notifier.repeat_minutes = int(self.config_data.get("notify_repeat_minutes", 120))
        self.monitor = Monitor(self.config_data, self._push_event, self.check_lock, self.notifier)
        self.monitor.start()
        self.start_button.configure(text="Стоп")
        self.log(
            f"Слежение запущено: проверка каждые {self.config_data['interval_minutes']} мин., "
            f"ищем дату не позже {self.config_data['deadline']}."
        )

    def check_now(self) -> None:
        if not self._collect_settings():
            return
        if self.monitor and self.monitor.is_alive():
            self.monitor.check_now()
            self.log("Проверка запущена вне очереди.")
            return
        if self.check_lock.locked():
            self.log("Проверка уже идёт, подождите.")
            return
        threading.Thread(target=self._single_check, daemon=True).start()

    def _single_check(self) -> None:
        with self.check_lock:
            self._push_event("started", None)
            try:
                round_result = run_check(
                    self.config_data, lambda message: self._push_event("log", message), self.notifier
                )
                self._push_event("finished", round_result)
            except Exception as exc:
                self._push_event("log", f"Проверка прервана: {type(exc).__name__}: {exc}")
                self._push_event("finished", None)

    def open_selected_site(self) -> None:
        site = self._selected_site()
        if site is None:
            messagebox.showinfo("Выберите сайт", "Сначала выделите строку с сайтом в таблице.")
            return
        webbrowser.open(site.get("url", ""))

    def toggle_selected_site(self) -> None:
        site = self._selected_site()
        if site is None:
            messagebox.showinfo("Выберите сайт", "Сначала выделите строку с сайтом в таблице.")
            return
        site["enabled"] = not site.get("enabled", True)
        name = site.get("name") or site.get("url", "")
        self.log(f"Сайт «{name}»: {'включён' if site['enabled'] else 'выключен'}")
        self._refresh_table()

    def open_config_file(self) -> None:
        if not CONFIG_PATH.exists():
            try:
                save_config(self.config_data)
            except OSError as exc:
                messagebox.showerror("Не удалось создать файл", str(exc))
                return
        try:
            if SYSTEM == "Windows":
                subprocess.Popen(["notepad", str(CONFIG_PATH)])
            elif SYSTEM == "Darwin":
                subprocess.Popen(["open", "-t", str(CONFIG_PATH)])
            else:
                subprocess.Popen(["xdg-open", str(CONFIG_PATH)])
            self.log("Файл настроек открыт. После правок нажмите «Перечитать» — то есть перезапустите программу.")
        except OSError as exc:
            messagebox.showerror("Не удалось открыть файл", f"{exc}\n\nОткройте вручную: {CONFIG_PATH}")

    def install_browser(self) -> None:
        """Скачать Chromium для Playwright — разово, при первом запуске."""
        if not messagebox.askyesno(
            "Установка браузера",
            "Сейчас будет скачан браузер Chromium (около 150 МБ).\n"
            "Это нужно один раз. Продолжить?",
        ):
            return
        threading.Thread(target=self._install_browser_worker, daemon=True).start()

    def _install_browser_worker(self) -> None:
        self._push_event("log", "Устанавливаю Playwright и браузер, это займёт несколько минут…")
        steps = (
            [sys.executable, "-m", "pip", "install", "--upgrade", "playwright"],
            [sys.executable, "-m", "playwright", "install", "chromium"],
        )
        for step in steps:
            try:
                completed = subprocess.run(step, capture_output=True, text=True, timeout=1800)
            except Exception as exc:
                self._push_event("log", f"Ошибка установки: {type(exc).__name__}: {exc}")
                return
            if completed.returncode != 0:
                tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-5:]
                self._push_event("log", "Установка не удалась: " + " | ".join(tail))
                return
        self._push_event("log", "Браузер установлен. Можно проверять сайты.")

    # ------------------------------------------------------------------
    # События фонового потока
    # ------------------------------------------------------------------
    def _push_event(self, kind: str, payload) -> None:
        self.events.put((kind, payload))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        finally:
            self.after(150, self._drain_events)

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "log":
            self.log(str(payload))
        elif kind == "started":
            self.next_check_at = None
            self._set_banner("Проверяю сайты…", COLOR_BUSY)
            self.check_button.configure(state="disabled")
        elif kind == "finished":
            self.check_button.configure(state="normal")
            self._show_round(payload)
        elif kind == "next":
            self.next_check_at = dt.datetime.now() + dt.timedelta(seconds=int(payload))

    def _show_round(self, round_result) -> None:
        if round_result is None:
            self._set_banner("Проверка не удалась — смотрите журнал", COLOR_ERROR)
            return

        self._refresh_table(round_result)

        if round_result.matches:
            first = round_result.matches[0]
            text = f"ЕСТЬ ЗАПИСЬ! {first.site} — {fmt_date(first.date)}"
            if len(round_result.matches) > 1:
                text += f" (и ещё {len(round_result.matches) - 1})"
            self._set_banner(text, COLOR_HIT)
            if round_result.new_matches:
                self._raise_window()
        elif round_result.has_errors:
            self._set_banner("Часть сайтов не проверилась — смотрите журнал", COLOR_ERROR)
        else:
            self._set_banner(
                f"Подходящих дат нет (нужна не позже {self.config_data['deadline']})", COLOR_MISS
            )

    def _refresh_table(self, round_result=None) -> None:
        by_name = {}
        if round_result is not None:
            by_name = {result.name: result for result in round_result.results}
            self._last_results = by_name

        cached = getattr(self, "_last_results", {}) if round_result is None else by_name
        deadline = None
        try:
            deadline = parse_deadline(self.config_data.get("deadline", ""))
        except ValueError:
            pass

        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        self.row_sites.clear()

        for index, site in enumerate(self.config_data.get("sites", [])):
            row_id = f"site-{index}"
            name = site.get("name") or site.get("url", "")
            self.row_sites[row_id] = site
            result = cached.get(name)

            if not site.get("enabled", True):
                values, tags = (name, "выключен", "—", "—", "—"), ("off",)
            elif result is None:
                values, tags = (name, "ещё не проверялся", "—", "—", "—"), ()
            elif not result.ok:
                values, tags = (name, f"ошибка: {result.error[:60]}", "—", "—",
                                result.checked_at.strftime("%H:%M:%S")), ("error",)
            else:
                hits = len(result.matches(deadline)) if deadline else 0
                status = "есть подходящая дата" if hits else "свободных дат нет" if not result.all_dates else "дат нет в нужный срок"
                values = (
                    name,
                    status,
                    fmt_date(result.earliest),
                    str(hits),
                    result.checked_at.strftime("%H:%M:%S"),
                )
                tags = ("hit",) if hits else ()

            self.tree.insert("", "end", iid=row_id, values=values, tags=tags)

        # Выделение переживает перерисовку таблицы — иначе кнопки под ней
        # начинают требовать выбрать строку заново.
        still_there = [row_id for row_id in selected if self.tree.exists(row_id)]
        if still_there:
            self.tree.selection_set(still_there)

    def _selected_site(self) -> dict | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.row_sites.get(selection[0])

    def _set_banner(self, text: str, color: str) -> None:
        self.banner.configure(text=text, bg=color)

    def _on_resize(self, event) -> None:
        if event.widget is self:
            self.banner.configure(wraplength=max(320, event.width - 60))

    def _raise_window(self) -> None:
        """Поднять окно поверх остальных — чтобы находку точно заметили."""
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.bell()
            self.after(4000, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _tick_countdown(self) -> None:
        if self.monitor and self.monitor.is_alive():
            if self.next_check_at:
                left = int((self.next_check_at - dt.datetime.now()).total_seconds())
                left = max(0, left)
                self.status_var.set(f"Слежение включено. Следующая проверка через {left // 60:02d}:{left % 60:02d}")
            else:
                self.status_var.set("Слежение включено. Идёт проверка…")
        elif self.check_lock.locked():
            self.status_var.set("Идёт разовая проверка…")
        else:
            self.status_var.set("Остановлено")
        self.after(1000, self._tick_countdown)

    # ------------------------------------------------------------------
    # Журнал
    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"

        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        extra = int(self.log_text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log_text.delete("1.0", f"{extra + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

        try:
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
        except OSError:
            pass

    def on_close(self) -> None:
        if self.monitor and self.monitor.is_alive():
            self.monitor.stop()
        if self._collect_settings():
            try:
                save_config(self.config_data)
            except OSError:
                pass
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
