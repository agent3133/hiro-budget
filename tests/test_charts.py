"""Chart helpers: axis ticks (0 on a grid line, readable steps), labels, theme colours."""

import pytest
from PySide6.QtCore import QLocale
from PySide6.QtGui import QColor

from hiro_budget.gui import charts, theme
from hiro_budget.gui.charts import axis_ticks, money_axis, nice_step


@pytest.fixture
def german():
    old = QLocale()
    QLocale.setDefault(QLocale("de"))
    yield
    QLocale.setDefault(old)


@pytest.mark.parametrize(
    ("low", "high"),
    [(-4000, 10000), (-80000, 250000), (-3.2, 7.9), (-120, 0), (0, 6300), (-17, 1234567)],
)
def test_zero_is_a_tick_and_steps_are_readable(low, high) -> None:
    ticks = axis_ticks(low, high)
    assert 0 in ticks
    assert ticks[0] <= low and ticks[-1] >= high
    step = ticks[1] - ticks[0]
    mantissa = step / 10 ** len(str(int(step))[:-1]) if step >= 1 else None
    if mantissa is not None:
        assert mantissa in (1, 2, 2.5, 5, 10)
    assert len(ticks) <= 7


def test_issue_34_example() -> None:
    # Overview "income and expenses per month": the range -4000 .. 10000 had ticks
    # -4000, -500, 3000, 6500, 10000 (no 0)
    ticks = axis_ticks(-4000 * 1.05, 10000 * 1.05)
    assert ticks == [-5000, 0, 5000, 10000, 15000]


def test_nice_step() -> None:
    assert nice_step(14000) == 5000
    assert nice_step(1000) == 200
    assert nice_step(12) == 2.5


def test_money_axis_labels_have_thousands_separators(qapp, german) -> None:
    axis = money_axis([-75_000, 240_000])
    labels = axis.categoriesLabels()
    assert "0" in labels
    assert "-100.000" in labels or "−100.000" in labels
    assert "300.000" in labels
    assert axis.min() == -100_000 and axis.max() == 300_000


def test_money_axis_tight_and_empty(qapp) -> None:
    tight = money_axis([159_000, 175_000], tight=True)
    assert 150_000 < tight.max() and tight.min() >= 150_000
    empty = money_axis([])
    assert (empty.min(), empty.max()) == (0, 100)


@pytest.mark.parametrize("name", ["rose_pine", "rose_pine_dark", "light"])
def test_chart_colours_come_from_the_theme(qapp, monkeypatch, name) -> None:
    monkeypatch.setattr(theme, "_active", name)
    for token in ("income", "expense", "principal", "line_series", "second_year", "neg_text"):
        assert theme.color(token).startswith("#")
    assert theme.series_color(0) != theme.series_color(1)
    assert theme.series_color(len(theme.color("series"))) == theme.series_color(0)


@pytest.mark.parametrize(("name", "dark"), [("rose_pine", False), ("rose_pine_dark", True)])
def test_bars_outlined_in_card_colour_and_partial_month_faded(
    qapp, monkeypatch, name, dark
) -> None:
    # #53: QtCharts drew a white outline around every bar (bright on the dark card), and
    # the partial month at 40 % alpha turned dark on a dark card instead of pale
    monkeypatch.setattr(theme, "_active", name)
    monkeypatch.setattr(charts, "is_dark", lambda: dark)
    card = QColor(theme.tokens()["input"])
    bars = charts.new_bar_set("x", QColor(theme.color("expense")))
    for _ in range(3):
        bars.append(1)
    assert bars.pen().color() == card
    charts.mark_partial([bars], ["2026-08", "2026-09", "2026-10"], ("2026-10", None))
    pale = bars.selectedColor()
    assert pale.alpha() == 255  # opaque, so the card doesn't darken it
    assert bars.isBarSelected(2) and not bars.isBarSelected(0)
    if dark:  # paler than the bar would be blended towards the card: lighter than the card
        assert pale.lightness() > card.lightness() + 80
    else:  # between the bar and the light card
        assert bars.color().lightness() < pale.lightness() <= card.lightness()
