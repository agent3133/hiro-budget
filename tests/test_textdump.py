"""hiro-budget dump-pdf: the text importers see, optionally masked (#24)."""

from __future__ import annotations

from hiro_budget.__main__ import main
from hiro_budget.importers.textdump import mask


def test_mask_keeps_dates_and_amounts():
    text = (
        "IBAN DE12 3456 7890 1234 5678 90\n"
        "31.10.2026 Lastschrift max.muster@example.com -1.234,56\n"
        "Mandat:123456 Referenz 4711 Kundennr 12345678"
    )
    assert mask(text) == (
        "IBAN DE00 0000 0000 0000 0000 00\n"
        "31.10.2026 Lastschrift name@example.org -1.234,56\n"
        "Mandat:000000 Referenz 4711 Kundennr 00000000"
    )


def test_dump_pdf_prints_the_pages(statement_pdf, capsys):
    assert main(["dump-pdf", str(statement_pdf)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("===== page 1 =====\n")
    assert "===== page 2 =====" in out
    assert "Hausverwaltung Muster" in out


def test_dump_pdf_needs_no_configuration(tmp_path, capsys):
    missing = tmp_path / "nothing.pdf"
    assert main(["--config-dir", str(tmp_path / "none"), "dump-pdf", str(missing)]) == 1
    assert "cannot read" in capsys.readouterr().err
