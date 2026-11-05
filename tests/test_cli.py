"""The command line generated from the operations (#83), and the registry's rules."""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from hiro_budget import ops
from hiro_budget.__main__ import build_parser, main


def run(capsys, config_dir, *argv: str) -> tuple[int, str, str]:
    code = main(["--config-dir", str(config_dir), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- registry ---------------------------------------------------------------------------


def test_registry_rules():
    operations = ops.registry()
    names = [o.name for o in operations]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z]+(_[a-z]+)*", n) for n in names)
    paths = [o.cli for o in operations if o.cli]  # import, recategorize, train: no path
    assert len(paths) == len(set(paths))
    for o in operations:
        assert o.summary and o.description, o.name
        assert o.args.model_config.get("extra") == "forbid", o.name
        for field, info in o.args.model_fields.items():
            assert info.description, f"{o.name}.{field} has no description"


def test_cli_paths_do_not_clash_with_builtin_commands():
    builtin = {"gui", "import", "recategorize", "train", "demo", "dump-pdf"}
    assert not builtin & {o.cli[0] for o in ops.registry() if o.cli}


# --- the command line -------------------------------------------------------------------


@pytest.mark.parametrize("op", [o for o in ops.registry() if o.cli], ids=lambda o: o.name)
def test_help_of_every_command(op, capsys):
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args([*op.cli, "--help"])
    assert e.value.code == 0
    assert op.summary in capsys.readouterr().out


def test_every_read_command_prints_text_and_json(capsys, config_dir):
    commands = [
        ["overview"],
        ["accounts", "list"],
        ["budget", "show", "2026-03"],
        ["tx", "list", "--limit", "3"],
        ["categories", "list"],
        ["rules", "list"],
        ["rules", "test", "--purpose", "x"],
        ["report", "monthly"],
        ["report", "categories"],
        ["report", "years", "2025", "2026"],
        ["report", "payees"],
        ["report", "budget", "2026-03"],
        ["report", "net-worth"],
        ["report", "explain", "expenses"],
        ["imports", "list"],
    ]
    for argv in commands:
        code, out, err = run(capsys, config_dir, *argv)
        assert code == 0, (argv, err)
        assert out.strip()
        code, out, err = run(capsys, config_dir, *argv, "--json")
        assert code == 0, (argv, err)
        json.loads(out)


def test_json_has_dot_amounts(capsys, config_dir):
    code, out, _ = run(capsys, config_dir, "accounts", "list", "--json")
    assert code == 0
    data = json.loads(out)
    cash = next(a for a in data["accounts"] if a["name"] == "Bargeld")
    assert cash["balance"] == "100.00"  # the opening balance


def test_op_error_exits_with_1(capsys, config_dir):
    code, out, err = run(capsys, config_dir, "tx", "list", "--category", "nonsense")
    assert code == 1 and out == ""
    assert err.startswith("Error: unknown category 'nonsense'")


def test_invalid_value_exits_with_1(capsys, config_dir):
    code, _, err = run(capsys, config_dir, "budget", "show", "march")
    assert code == 1 and "YYYY-MM" in err
    code, _, err = run(capsys, config_dir, "tx", "list", "--limit", "0")
    assert code == 1 and "invalid arguments" in err


def test_usage_error_exits_with_2(capsys, config_dir):
    with pytest.raises(SystemExit) as e:
        main(["--config-dir", str(config_dir), "tx", "show"])  # the id is missing
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        main(["--config-dir", str(config_dir), "report"])  # a group without a command
    assert e.value.code == 2


def test_configuration_error_exits_with_2(capsys, config_dir):
    (config_dir / "config.yaml").write_text("language: [broken", encoding="utf-8")
    code, out, err = run(capsys, config_dir, "overview")
    assert code == 2 and out == ""
    assert "Configuration error" in err


def test_write_commands_take_german_negative_amounts_and_repeated_options(capsys, config_dir):
    code, out, err = run(
        capsys,
        config_dir,
        "tx",
        "add",
        "--account",
        "Bargeld",
        "--date",
        "2026-03-05",
        "--amount",
        "-50,00",
        "--payee",
        "Shop",
    )
    assert code == 0, err
    code, out, _ = run(capsys, config_dir, "tx", "list", "--json")
    (booking,) = json.loads(out)["bookings"]
    assert booking["amount"] == "-50.00"
    tx_id = str(booking["id"])
    code, out, err = run(
        capsys,
        config_dir,
        "tx",
        "split",
        tx_id,
        "--part",
        "lebensmittel.supermarkt=-40,00",
        "--part",
        "lebensmittel.drogerie=-10,00",
    )
    assert code == 0, err
    assert "2 parts" in out
    code, out, err = run(capsys, config_dir, "tx", "categorize", tx_id, tx_id, "Supermarkt")
    assert code == 0 and "1 bookings" in out, err
    code, out, err = run(capsys, config_dir, "tx", "delete", tx_id, "--json")
    assert code == 0 and json.loads(out)["ids"] == [int(tx_id)]


# --- startup stays light ----------------------------------------------------------------


def test_json_before_the_command_and_on_the_builtin_commands(capsys, config_dir, statement_pdf):
    """``--json`` works before a command as well as after it, and on import/recategorize/
    train, which have no generated command (#87)."""
    code, before, _ = run(capsys, config_dir, "--json", "categories", "list")
    _, after, _ = run(capsys, config_dir, "categories", "list", "--json")
    assert code == 0 and json.loads(before) == json.loads(after)
    code, out, _ = run(capsys, config_dir, "recategorize", "--json")
    assert code == 0 and json.loads(out)["message"].startswith("categorization")
    code, out, _ = run(capsys, config_dir, "--json", "import", str(statement_pdf))
    assert code == 0 and "1 of 1 files imported" in json.loads(out)["message"]
    code, out, err = run(capsys, config_dir, "train", "--json")  # too few bookings to train
    assert code == 1 and out == "" and err.startswith("Error: not enough training data")


def test_the_interactive_import_is_journaled(capsys, config_dir, statement_pdf):
    """The open window loads a change it finds in the journal (#85): the ``import`` command
    with its overlap question must write an entry like every other change (#87)."""
    from sqlalchemy import select

    from hiro_budget.app import App
    from hiro_budget.db.models import ChangeLog

    code, out, _ = run(capsys, config_dir, "import", str(statement_pdf))
    assert code == 0 and "backup:" in out
    with App.from_config_dir(config_dir).session() as s:
        entries = s.scalars(select(ChangeLog)).all()
        assert [(e.operation, e.source, e.undoable) for e in entries] == [
            ("import_files", "cli", False)
        ]


def test_the_window_and_import_do_not_build_the_operations_commands():
    """Starting the app pays no ~0.1 s for commands it does not run (#84)."""
    code = (
        "import sys; from hiro_budget.__main__ import _parse;"
        "[_parse(argv) for argv in ([], ['gui'], ['import', 'x.pdf'], ['train'])];"
        "print('hiro_budget.ops' in sys.modules, 'hiro_budget.ops.cli' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False False"


def test_help_does_not_import_heavy_modules():
    code = (
        "import sys; from hiro_budget.__main__ import build_parser;"
        "p = build_parser();"
        "import contextlib, io\n"
        "with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()):"
        " p.parse_args(['--help'])\n"
        "print(sorted(m for m in ('numpy', 'pdfplumber', 'openpyxl', 'alembic', 'sklearn')"
        " if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
