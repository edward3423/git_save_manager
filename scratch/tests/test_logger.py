import logging

from core.logger import LOGGER_NAME, FanoutHandler, configure_logging, log


def test_listeners_receive_formatted_messages():
    handler = FanoutHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    seen: list[str] = []
    handler.add_listener(seen.append)

    handler.emit(logging.LogRecord(LOGGER_NAME, logging.INFO, "", 0, "synced", None, None))

    assert seen == ["INFO synced"]


def test_removed_listeners_stop_receiving():
    handler = FanoutHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    seen: list[str] = []
    handler.add_listener(seen.append)
    handler.remove_listener(seen.append)  # a different bound object; must not raise
    handler.remove_listener(seen.append)

    listener = seen.append
    handler.add_listener(listener)
    handler.remove_listener(listener)
    handler.emit(logging.LogRecord(LOGGER_NAME, logging.INFO, "", 0, "x", None, None))

    assert seen == []


def test_a_broken_listener_cannot_break_logging():
    """The GUI console must never be able to take the application down with it."""
    handler = FanoutHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    seen: list[str] = []

    def explodes(_message: str) -> None:
        raise RuntimeError("the console blew up")

    handler.add_listener(explodes)
    handler.add_listener(seen.append)

    handler.emit(logging.LogRecord(LOGGER_NAME, logging.INFO, "", 0, "still logged", None, None))

    assert seen == ["still logged"]


def test_configure_logging_is_idempotent():
    logging.getLogger(LOGGER_NAME).handlers.clear()

    first = configure_logging()
    second = configure_logging()

    assert first is second
    assert len(logging.getLogger(LOGGER_NAME).handlers) == 2  # stdout + fanout, not four


def test_messages_logged_through_the_app_logger_reach_listeners():
    logging.getLogger(LOGGER_NAME).handlers.clear()
    fanout = configure_logging()
    seen: list[str] = []
    fanout.add_listener(seen.append)

    log().info("Sync complete")

    assert any("Sync complete" in line for line in seen)
