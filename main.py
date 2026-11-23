"""Entry point: `gsm` opens the window; `gsm <command>` runs one command and exits.

For the window: bring the core up, refuse early if we must, then open it. The two refusals
happen *before* the window exists - a read-only install and a second instance both get a
message box and a clean exit, not a half-started application.

A command never imports Qt at all (see `cli`), so it starts quickly and runs where no display
exists. `gsm gui` opens the window explicitly, passing any further arguments on to Qt.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    arguments = sys.argv[1:]
    if arguments and arguments[0] != "gui":
        from cli.main import main as run_command

        return run_command(arguments)
    return open_window([sys.argv[0], *arguments[1:]])


def open_window(argv: list[str]) -> int:
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from core import startup
    from core.lock import AlreadyRunning
    from core.logger import configure_logging
    from core.paths import Paths, WorkspaceNotWritable

    fanout = configure_logging()
    qt = QApplication(argv)
    qt.setStyleSheet((Path(__file__).parent / "ui" / "style.qss").read_text(encoding="utf-8"))

    from ui.shortcuts import WindowShortcuts

    shortcuts = WindowShortcuts(qt)  # Cmd+W / Cmd+Q, in every window and dialog
    qt.installEventFilter(shortcuts)

    try:
        app = startup.start(Paths.default())
    except (AlreadyRunning, WorkspaceNotWritable) as refusal:
        QMessageBox.critical(None, "Git Save Manager", str(refusal))
        return 1

    from PyQt6.QtCore import QTimer

    from ui.main_window import MainWindow

    window = MainWindow(app, fanout)
    window.show()
    if not app.config.is_set_up:
        # First run: open setup by itself, rather than leaving the user to find the button.
        QTimer.singleShot(0, window.set_up)
    try:
        return qt.exec()
    finally:
        app.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
