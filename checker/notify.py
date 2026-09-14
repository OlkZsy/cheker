"""Уведомления на компьютере: всплывающее окно и звук, без внешних зависимостей.

Windows — тост Windows 10/11 (с откатом на обычное окно), macOS — стандартное
уведомление, Linux — notify-send. Если ничего не сработало, приложение всё равно
покажет находку в своём окне и в логе.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading

SYSTEM = platform.system()

# Скрипт тоста для Windows. Текст передаётся через переменные окружения,
# чтобы не ломать кавычки и не подставлять чужой текст в команду.
_WINDOWS_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($env:CHECKER_TITLE)) > $null
$nodes.Item(1).AppendChild($xml.CreateTextNode($env:CHECKER_MESSAGE)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
Start-Sleep -Milliseconds 400
"""


def notify(title: str, message: str, sound: bool = True) -> bool:
    """Показать уведомление. Возвращает True, если система его приняла."""
    try:
        if SYSTEM == "Windows":
            shown = _notify_windows(title, message)
        elif SYSTEM == "Darwin":
            shown = _notify_macos(title, message, sound)
            sound = False  # звук уже включён в само уведомление
        else:
            shown = _notify_linux(title, message)
    except Exception:
        shown = False

    if sound:
        play_sound()
    return shown


def play_sound() -> None:
    """Короткий звуковой сигнал — чтобы заметить, даже не глядя на экран."""
    try:
        if SYSTEM == "Windows":
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        if SYSTEM == "Darwin":
            _run(["afplay", "/System/Library/Sounds/Glass.aiff"], wait=False)
            return
        for player, arg in (("paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"), ("beep", None)):
            if shutil.which(player):
                _run([player] + ([arg] if arg else []), wait=False)
                return
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass


def _notify_windows(title: str, message: str) -> bool:
    environment = dict(os.environ, CHECKER_TITLE=title, CHECKER_MESSAGE=message)
    completed = _run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-Command",
            _WINDOWS_TOAST_PS,
        ],
        env=environment,
        timeout=20,
    )
    if completed is not None and completed.returncode == 0:
        return True
    return _windows_message_box(title, message)


def _windows_message_box(title: str, message: str) -> bool:
    """Запасной вариант: обычное окно поверх всех, в отдельном потоке."""
    try:
        import ctypes

        MB_OK, MB_ICONINFORMATION, MB_SETFOREGROUND, MB_TOPMOST = 0x0, 0x40, 0x10000, 0x40000
        flags = MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
        threading.Thread(
            target=lambda: ctypes.windll.user32.MessageBoxW(0, message, title, flags),
            daemon=True,
        ).start()
        return True
    except Exception:
        return False


def _notify_macos(title: str, message: str, sound: bool) -> bool:
    script = (
        f"display notification {_applescript_quote(message)} "
        f"with title {_applescript_quote(title)}"
    )
    if sound:
        script += ' sound name "Glass"'
    completed = _run(["osascript", "-e", script], timeout=20)
    return completed is not None and completed.returncode == 0


def _notify_linux(title: str, message: str) -> bool:
    if not shutil.which("notify-send"):
        return False
    completed = _run(
        ["notify-send", "--urgency=critical", "--app-name=Pasport Checker", title, message],
        timeout=20,
    )
    return completed is not None and completed.returncode == 0


def _applescript_quote(text: str) -> str:
    return '"' + (text or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run(command: list[str], env=None, timeout: int | None = 20, wait: bool = True):
    """Запустить команду без мигающего чёрного окна на Windows."""
    creationflags = 0x08000000 if SYSTEM == "Windows" else 0  # CREATE_NO_WINDOW
    try:
        if not wait:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return None
        return subprocess.run(
            command,
            env=env,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            check=False,
        )
    except Exception:
        return None
