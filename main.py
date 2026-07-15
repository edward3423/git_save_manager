"""Entry point: bring the core up, refuse early if we must, then open the window.

The two refusals happen *before* the window exists - a read-only install and a second
instance both get a message box and a clean exit, not a half-started application.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QMessageBox

from core import startup
from core.lock import AlreadyRunning
from core.logger import configure_logging
from core.paths import Paths, WorkspaceNotWritable


def main() -> int:
    fanout = configure_logging()
    qt = QApplication(sys.argv)
    qt.setStyleSheet((Path(__file__).parent / "ui" / "style.qss").read_text(encoding="utf-8"))

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
