"""Cash flow of a period as a Sankey diagram: income category -> income group -> expense
group -> expense category.

Income and expenses follow ``reports`` (no transfers between own accounts, reporting
month, one-offs). Each node is a net sum; categories whose net sum goes the wrong way (a
refund larger than the spending) cannot be drawn as a flow and are listed in
``left_out``. Categories below ``MERGE_SHARE`` of the total are merged into one "other"
node per group. The difference of income and expenses is a balance node: a surplus on the
expense side, a deficit ("from reserves") on the income side, so both sides are equally
high. Money from the income groups to the expense groups is not traceable; it is split in
proportion (exact when there is one income group).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy.orm import Session

from hiro_budget.db.models import Category, CategoryGroup, Split
from hiro_budget.services.reports import UNCATEGORIZED_KEY, is_income, report_splits

MERGE_SHARE = 0.01  # categories below 1 % of the total are merged per group

# columns
INCOME_CATEGORY, INCOME_GROUP, EXPENSE_GROUP, EXPENSE_CATEGORY = range(4)

NodeKind = Literal["category", "group", "other", "uncategorized", "surplus", "deficit"]


@dataclass
class Node:
    column: int
    kind: NodeKind
    name: str  # category or group name; the group's name for "other"; "" for the rest
    cents: int
    income: bool  # on the income side (columns 0, 1)
    category_ids: list[int | None] = field(default_factory=list)  # for a breakdown
    one_off: bool = False
    group_id: int | None = None  # the group of a category, "other" or group node


@dataclass
class Link:
    source: int  # node index
    target: int
    cents: int


@dataclass
class LeftOut:
    name: str  # "group: category"
    cents: int  # the net sum, signed as a flow (negative)
    income: bool


@dataclass
class CashFlow:
    nodes: list[Node]
    links: list[Link]
    income_cents: int  # sum of the drawn income flows
    expense_cents: int
    left_out: list[LeftOut] = field(default_factory=list)  # net sums against the flow
    one_off_income_cents: int = 0  # of one-off categories (counted or left out)
    one_off_expense_cents: int = 0
    income_groups: int = 0  # > 1: the middle links are proportional

    @property
    def total_cents(self) -> int:
        """Height of each side."""
        return max(self.income_cents, self.expense_cents)

    @property
    def balance_cents(self) -> int:
        return self.income_cents - self.expense_cents


@dataclass
class _Cat:
    category_id: int | None
    name: str
    group: str
    group_sort: tuple[int, int]
    one_off: bool
    cents: int = 0  # signed as a flow: income positive, expenses positive


def cash_flow(
    session: Session,
    date_from: date | None = None,
    date_to: date | None = None,
    account_id: int | None = None,
    one_offs: bool = True,
) -> CashFlow:
    q = report_splits(
        Split.amount_cents,
        Split.category_id,
        Category.key,
        Category.name,
        Category.one_off,
        CategoryGroup.name,
        CategoryGroup.sort,
        CategoryGroup.id,
        CategoryGroup.is_income,
        date_from=date_from,
        date_to=date_to,
        account_id=account_id,
    )
    cats: dict[tuple[bool, str], _Cat] = {}
    one_off_income = one_off_expense = 0
    for (
        amount,
        cat_id,
        key,
        name,
        one_off,
        group,
        group_sort,
        group_id,
        income_group,
    ) in session.execute(q):
        income = is_income(amount, cat_id, income_group)
        flow = amount if income else -amount
        if one_off:
            if income:
                one_off_income += flow
            else:
                one_off_expense += flow
            if not one_offs:
                continue
        c = cats.setdefault(
            (income, key or UNCATEGORIZED_KEY),
            _Cat(cat_id, name or "", group or "", (group_sort or 0, group_id or 0), bool(one_off)),
        )
        c.cents += flow

    left_out = [
        LeftOut(f"{c.group}: {c.name}", c.cents, income)
        for (income, _k), c in cats.items()
        if c.cents < 0
    ]
    income_cats = [c for (inc, _k), c in cats.items() if inc and c.cents > 0]
    expense_cats = [c for (inc, _k), c in cats.items() if not inc and c.cents > 0]
    income_cents = sum(c.cents for c in income_cats)
    expense_cents = sum(c.cents for c in expense_cats)
    total = max(income_cents, expense_cents)

    nodes: list[Node] = []
    links: list[Link] = []

    def add(node: Node) -> int:
        nodes.append(node)
        return len(nodes) - 1

    def side(categories: list[_Cat], income: bool) -> list[int]:
        """Category and group nodes of one side; returns the group nodes (largest first)."""
        group_col, cat_col = (
            (INCOME_GROUP, INCOME_CATEGORY) if income else (EXPENSE_GROUP, EXPENSE_CATEGORY)
        )
        by_group: dict[tuple[int, int], list[_Cat]] = {}
        uncategorized = None
        for c in categories:
            if c.category_id is None:
                uncategorized = c
            else:
                by_group.setdefault(c.group_sort, []).append(c)
        groups = sorted(
            by_group.values(), key=lambda cs: (-sum(c.cents for c in cs), cs[0].group_sort)
        )
        group_nodes = []
        for members in groups:
            members.sort(key=lambda c: -c.cents)
            ids = [c.category_id for c in members]
            name, group_id = members[0].group, members[0].group_sort[1]
            cents = sum(c.cents for c in members)
            g = add(Node(group_col, "group", name, cents, income, ids, group_id=group_id))
            group_nodes.append(g)
            small = [c for c in members if c.cents < total * MERGE_SHARE]
            if len(small) < 2 or len(small) == len(members):  # "other" would be the group
                small = []
            for c in members:
                if c in small:
                    continue
                ids = [c.category_id]
                n = add(
                    Node(cat_col, "category", c.name, c.cents, income, ids, c.one_off, group_id)
                )
                links.append(Link(n, g, c.cents) if income else Link(g, n, c.cents))
            if small:
                cents = sum(c.cents for c in small)
                ids = [c.category_id for c in small]
                n = add(Node(cat_col, "other", name, cents, income, ids, group_id=group_id))
                links.append(Link(n, g, cents) if income else Link(g, n, cents))
        if uncategorized is not None:  # no category level: a node in the group column
            group_nodes.append(
                add(Node(group_col, "uncategorized", "", uncategorized.cents, income, [None]))
            )
        return group_nodes

    sources = side(income_cats, True)
    targets = side(expense_cats, False)
    balance = income_cents - expense_cents
    if balance > 0:
        targets.append(add(Node(EXPENSE_GROUP, "surplus", "", balance, False)))
    elif balance < 0:
        sources.append(add(Node(INCOME_GROUP, "deficit", "", -balance, True)))
    links.extend(_split_middle(nodes, sources, targets))
    return CashFlow(
        nodes,
        links,
        income_cents,
        expense_cents,
        sorted(left_out, key=lambda o: o.cents),
        one_off_income,
        one_off_expense,
        len({c.group_sort for c in income_cats if c.category_id is not None}),
    )


def _split_middle(nodes: list[Node], sources: list[int], targets: list[int]) -> list[Link]:
    """Links from every source to every target in proportion to the targets; each source
    gives away exactly its sum (the remainder goes to its largest share)."""
    total = sum(nodes[t].cents for t in targets)
    if not total:
        return []
    links = []
    for s in sources:
        cents = nodes[s].cents
        shares = [cents * nodes[t].cents // total for t in targets]
        largest = max(range(len(targets)), key=lambda i: shares[i])
        shares[largest] += cents - sum(shares)
        links.extend(Link(s, t, c) for t, c in zip(targets, shares, strict=True) if c > 0)
    return links
