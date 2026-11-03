"""The Claude Code hooks in .claude/hooks: the test runner's short, de-duplicated reports and
its pause, and the CRLF check."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / ".claude" / "hooks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN_TESTS = _load("run_tests")
CHECK_CRLF = _load("check_crlf")


def pytest_output(address: str, tmp: str, line: int, seconds: str, error: str) -> str:
    """The shape of ``pytest -x -q`` output with one failure and a long traceback."""
    traceback = "\n".join(f"    frame {i} in some_module.py" for i in range(60))
    return f"""\
.F
================================== FAILURES ===================================
__________________________ test_dialog_saves __________________________

app = <hiro_budget.app.App object at {address}>
statement_pdf = WindowsPath('C:/Users/x/AppData/Local/Temp/pytest-of-x/{tmp}/a.pdf')
{traceback}
>       assert dlg.save()
E       {error}
E         more detail of the error
E         and more

src\\hiro_budget\\gui\\bank_dialog.py:{line}: {error.split(":")[0]}
=========================== short test summary info ===========================
FAILED tests/test_bank_gui.py::test_dialog_saves - {error}
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed, 1 passed in {seconds}s
"""


def test_report_is_short():
    lines = pytest_output("0x01", "pytest-7", 120, "3.02", "NameError: x").splitlines()
    report = RUN_TESTS.short_report(lines)
    assert report == [
        "FAILED tests/test_bank_gui.py::test_dialog_saves - NameError: x",
        "E       NameError: x",
        "E         more detail of the error",
        "E         and more",
        "1 failed, 1 passed in 3.02s",
    ]
    assert len(lines) > 70  # what the hook printed before (the last 40 lines)


def test_conftest_crash_without_e_lines_keeps_the_last_lines():
    lines = [
        "ImportError while loading conftest 'tests/conftest.py'.",
        "x.py:3: in <module>",
        "    import missing",
        "E-less: ModuleNotFoundError: No module named 'missing'",
    ]
    assert RUN_TESTS.short_report(lines)[-1].endswith("No module named 'missing'")


def test_same_failure_has_the_same_signature():
    """Addresses, temp folders, line numbers and timings change between runs of one
    failure; another error does not have the same signature."""
    a = RUN_TESTS.short_report(
        pytest_output("0x01AB", "pytest-1607", 120, "3.02", "NameError: x").splitlines()
    )
    b = RUN_TESTS.short_report(
        pytest_output("0x7F00", "pytest-1612", 131, "2.98", "NameError: x").splitlines()
    )
    c = RUN_TESTS.short_report(
        pytest_output("0x01AB", "pytest-1607", 120, "3.02", "NameError: y").splitlines()
    )
    assert RUN_TESTS.signature(a) == RUN_TESTS.signature(b)
    assert RUN_TESTS.signature(a) != RUN_TESTS.signature(c)


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """The test hook on a scratch project with tests/test_x.py; ``call(path)`` returns
    (exit code, stderr) and counts pytest runs (pytest itself is faked)."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(RUN_TESTS, "ROOT", tmp_path)
    monkeypatch.setattr(RUN_TESTS, "TESTS", tmp_path / "tests")
    monkeypatch.setattr(RUN_TESTS, "STATE", tmp_path / ".claude" / "hook_state")
    monkeypatch.setattr(
        RUN_TESTS, "LAST_FAILURE", tmp_path / ".claude" / "hook_state" / "last_test_failure.txt"
    )
    monkeypatch.setattr(RUN_TESTS, "PAUSE", tmp_path / ".claude" / "pause_tests")
    state = {"runs": 0, "output": "", "code": 1}

    def fake_run(cmd, **kwargs):
        state["runs"] += 1
        return subprocess.CompletedProcess(cmd, state["code"], state["output"], "")

    monkeypatch.setattr(RUN_TESTS.subprocess, "run", fake_run)

    def call(path: Path) -> tuple[int, str]:
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"tool_input": {"file_path": str(path)}}))
        )
        err = io.StringIO()
        monkeypatch.setattr("sys.stderr", err)
        return RUN_TESTS.main(), err.getvalue()

    return tmp_path, state, call


def test_a_repeated_failure_is_reported_in_one_line(hook):
    root, state, call = hook
    test = root / "tests" / "test_x.py"
    state["output"] = pytest_output("0x01", "pytest-1", 10, "1.00", "NameError: x")
    code, first = call(test)
    assert code == 2 and "E       NameError: x" in first and len(first.splitlines()) == 6
    state["output"] = pytest_output("0x02", "pytest-2", 12, "1.10", "NameError: x")
    code, again = call(test)
    assert code == 2 and len(again.splitlines()) == 1 and "still fail as before" in again
    state["output"] = pytest_output("0x02", "pytest-2", 12, "1.10", "TypeError: y")
    code, other = call(test)
    assert "TypeError: y" in other and "still" not in other  # another failure: in full
    state["code"], state["output"] = 0, "2 passed in 0.1s"
    assert call(test)[0] == 0
    state["code"], state["output"] = 1, pytest_output("0x09", "pytest-9", 99, "9.0", "TypeError: y")
    assert "still" not in call(test)[1]  # after a pass the same failure is news again


def test_paused_tests_do_not_run_and_are_listed(hook):
    root, state, call = hook
    test = root / "tests" / "test_x.py"
    pause = root / ".claude" / "pause_tests"
    pause.parent.mkdir()
    pause.write_text("", encoding="utf-8")
    assert call(test) == (0, "") and call(test) == (0, "")
    assert state["runs"] == 0
    assert pause.read_text(encoding="utf-8").split() == ["tests/test_x.py"]  # once
    pause.unlink()
    state["code"], state["output"] = 0, "1 passed in 0.1s"
    assert call(test)[0] == 0 and state["runs"] == 1


def test_crlf_found_only_in_text_files_changed_since_the_last_check(tmp_path):
    old = tmp_path / "old.py"
    old.write_bytes(b"a = 1\r\n")
    os.utime(old, (1000, 1000))  # changed before the last check
    new_crlf, new_lf = tmp_path / "new.md", tmp_path / "ok.py"
    new_crlf.write_bytes(b"# x\r\n")
    new_lf.write_bytes(b"a = 1\n")
    binary = tmp_path / "app_de.qm"
    binary.write_bytes(b"\x00\r\n")
    files = [old, new_crlf, new_lf, binary]
    assert CHECK_CRLF.with_crlf(files, since=2000) == [new_crlf]
