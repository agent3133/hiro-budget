"""Budget vs. actual: what was assigned to each budget line and what was spent.

Spent is the negative activity of a budget line (refunds reduce it), so
``assigned - spent`` is what a line saved (positive) or overspent (negative) over the
months, independent of money carried over from before.

As in the other reports, spending counts in its reporting month (``report_shift_days``,
see ``reports``), and one-off categories can be left out: their own lines go, and inside a
group budgeted as a whole their spending does not count.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from hiro_budget.config import Settings
from hiro_budget.db.models import Category
from hiro_budget.services import budget


@dataclass
class LineActual:
    """One budget line (category, or group budgeted as a whole) over the months."""

    key: str
    name: str
    group_name: str
    is_group: bool
    assigned: int = 0
    spent: int = 0
    one_off: bool = False
    # categories whose spending counts (for the breakdown, see drilldown.budget_breakdown)
    category_ids: tuple[int, ...] = ()

    @property
    def difference(self) -> int:
        """Positive: less spent than assigned; negative: overspent."""
        return self.assigned - self.spent

    @property
    def used(self) -> float | None:
        """Share of the assigned money that was spent (None without assignments, or when
        refunds exceed the spending)."""
        return self.spent / self.assigned if self.assigned > 0 and self.spent >= 0 else None


@dataclass
class MonthActual:
    month: str
    assigned: int = 0
    spent: int = 0


@dataclass
class BudgetActual:
    months: list[MonthActual] = field(default_factory=list)
    lines: list[LineActual] = field(default_factory=list)  # budget order
    one_off_spent: int = 0  # by one-off categories in these months (left out or not)

    @property
    def category_ids(self) -> list[int]:
        return [c for line in self.lines for c in line.category_ids]

    @property
    def assigned(self) -> int:
        return sum(m.assigned for m in self.months)

    @property
    def spent(self) -> int:
        return sum(m.spent for m in self.months)


def budget_vs_actual(
    session: Session,
    settings: Settings,
    first_month: str,
    last_month: str,
    only_budgeted: bool = False,
    one_offs: bool = True,
) -> BudgetActual:
    """Months from ``first_month`` to ``last_month``, limited to the budget's start month;
    ``only_budgeted`` skips months in which nothing was assigned."""
    start = budget.start_month(session, settings)
    first_month = max(first_month, start)
    result = BudgetActual()
    if first_month > last_month:
        return result
    lines: dict[str, LineActual] = {}
    data = budget.BudgetData.load(session, settings)  # once for all months
    one_off = set(session.scalars(select(Category.id).where(Category.one_off.is_(True))))
    left_out = set() if one_offs else one_off
    envelope_of = budget._budget_lines(session, settings).of_category
    categories_of: dict[budget.Envelope, list[int]] = defaultdict(list)
    for cat_id, env in envelope_of.items():
        if cat_id not in left_out:
            categories_of[env].append(cat_id)
    spent: dict[tuple[budget.Envelope, str], int] = defaultdict(int)
    one_off_spent: dict[str, int] = defaultdict(int)
    for (cat_id, month), cents in data.flows.report_activity.items():
        if cat_id in one_off:
            one_off_spent[month] -= cents
        if cat_id not in left_out:
            spent[(envelope_of[cat_id], month)] -= cents
    for b in budget.compute_months(  # one pass over all months (#59)
        session, settings, first_month, last_month, data, balances=False
    ):
        month = b.month
        envelopes = [e for e in b.envelopes if e.is_group or e.category_id not in left_out]
        if only_budgeted and not any(e.assigned for e in envelopes):
            continue
        total = MonthActual(month)
        result.one_off_spent += one_off_spent.get(month, 0)
        for e in envelopes:
            line = lines.setdefault(
                e.key,
                LineActual(
                    e.key,
                    e.name,
                    e.group_name,
                    e.is_group,
                    one_off=not e.is_group and e.category_id in one_off,
                    category_ids=tuple(sorted(categories_of[e.envelope])),
                ),
            )
            line_spent = spent.get((e.envelope, month), 0)
            line.assigned += e.assigned
            line.spent += line_spent
            total.assigned += e.assigned
            total.spent += line_spent
        result.months.append(total)
    result.lines = [line for line in lines.values() if line.assigned or line.spent]
    return result
