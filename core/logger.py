"""Logging that fans out to stdout and to any number of listeners.

The plan called for `core` to emit a PyQt signal directly. It doesn't: `core` stays free of
Qt entirely, and instead exposes a listener mechanism that the GUI adapts into a signal.
Otherwise the modules capable of losing save data would drag a GUI toolkit into every test.

Never log a PAT. Credentials are passed to Git through the subprocess environment precisely
so they never reach a file, a process list, or this console.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable

LOGGER_NAME = "gsm"
_FORMAT = "%(asctime)s  %(levelname)-7s  %(message)s"
_TIME_FORMAT = "%H:%M:%S"

LogListener = Callable[[str], None]


class FanoutHandler(logging.Handler):
    """Delivers formatted records to every registered listener.

    A listener that raises is ignored: the GUI console must never be able to take the
    logger, and therefore the application, down with it.
    """

    def __init__(self) -> None:
        super().__init__()
        self._listeners: list[LogListener] = []

    def add_listener(self, listener: LogListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: LogListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        for listener in list(self._listeners):
            # A broken listener must not break logging, and must not stop the other
            # listeners from receiving the message.
            with contextlib.suppress(Exception):
                listener(message)


def configure_logging(level: int = logging.INFO) -> FanoutHandler:
    """Attach stdout and fanout handlers to the app logger. Idempotent.

    Returns the fanout handler so the GUI can subscribe to it.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in logger.handlers:
        if isinstance(handler, FanoutHandler):
            return handler

    formatter = logging.Formatter(_FORMAT, datefmt=_TIME_FORMAT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    fanout = FanoutHandler()
    fanout.setFormatter(formatter)
    logger.addHandler(fanout)
    return fanout


def log() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
