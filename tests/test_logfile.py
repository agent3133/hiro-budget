"""Errors keep their traceback (#79): show_error logs, the excepthook, the log file."""

from __future__ import annotations

import logging
import sys

import pytest
from PySide6.QtWidgets import QMessageBox

from hiro_budget import logfile
from hiro_budget.config import load_settings
from hiro_budget.gui import common
from hiro_budget.gui import main as gui_main


@pytest.fixture
def settings(tmp_path):
    yield load_settings(tmp_path / "config")
    logfile.close_log_file()


@pytest.fixture
def boxes(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: shown.append(a[1:]))
    return shown


def _raise(error: Exception) -> Exception:
    try:
        raise error
    except Exception as e:
        return e


def test_show_error_logs_the_traceback(qapp, caplog, boxes):
    error = _raise(KeyError("x"))
    with caplog.at_level(logging.ERROR):
        common.show_error(None, "Export", error)
    (record,) = caplog.records
    assert record.exc_info[1] is error and "Export" in record.getMessage()
    assert boxes == [("Export", "'x'")]
    common.show_error(None, "Export", _raise(KeyError()))
    assert boxes[-1] == ("Export", "KeyError")  # not an empty message


def test_the_log_file_is_in_the_data_folder(settings):
    path = logfile.open_log_file(settings)
    assert path == (settings.data_dir / "logs" / "hiro-budget.log").resolve()
    assert logfile.open_log_file(settings) == path  # one handler only
    logging.getLogger("hiro_budget.test").info("not written")
    logging.getLogger("hiro_budget.test").warning("written")
    logfile.close_log_file()
    text = path.read_text(encoding="utf-8")
    assert "written" in text and "not written" not in text


def test_excepthook_logs_and_shows_a_message(qapp, settings, boxes):
    path = logfile.open_log_file(settings)
    error = _raise(ZeroDivisionError("division by zero"))
    gui_main.excepthook(type(error), error, error.__traceback__)
    logfile.close_log_file()
    text = path.read_text(encoding="utf-8")
    assert "CRITICAL" in text and "Traceback" in text and "ZeroDivisionError" in text
    ((title, message),) = boxes
    assert title == "Unexpected error"
    assert "division by zero" in message and str(path) in message


def test_excepthook_leaves_keyboard_interrupt_alone(monkeypatch, boxes):
    seen = []
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: seen.append(a[0]))
    gui_main.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert seen == [KeyboardInterrupt] and boxes == []
