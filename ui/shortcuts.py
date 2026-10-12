"""Close Window and Quit, on the platform's own keys: Cmd+W and Cmd+Q on macOS, Ctrl+W and
Ctrl+Q on Linux, and Ctrl+W on Windows, which has no standard Quit key.

Handled by one filter on the whole application rather than by a shortcut on each window,
because Qt disables a window's shortcuts while a dialog is open over it - and an open dialog
is exactly when Close Window is reached for. The filter sees the key before any widget does,
so it works the same in the main window, in every dialog, and with focus in a text box.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtGui import QKeyEvent, QKeySequence
from PyQt6.QtWidgets import QApplication


class WindowShortcuts(QObject):
    """Install once, on the QApplication."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt's name
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return False
        if event.matches(QKeySequence.StandardKey.Close):
            close_active_window()
            return True
        if event.matches(QKeySequence.StandardKey.Quit):
            quit_application()
            return True
        return False


def close_active_window() -> None:
    """Close the window in front: a dialog closes as if cancelled, and the main window closes
    the application, as it would from its title bar."""
    active = QApplication.activeWindow()
    if active is not None:
        active.close()


def quit_application() -> None:
    """Close every window, front to back, then leave. A dialog closes as if cancelled, so any
    operation it was asking about simply does not happen."""
    while (active := QApplication.activeModalWidget()) is not None:
        active.close()
        if QApplication.activeModalWidget() is active:
            return  # it refused to close; leave it to the user
    QApplication.closeAllWindows()
    QApplication.quit()
