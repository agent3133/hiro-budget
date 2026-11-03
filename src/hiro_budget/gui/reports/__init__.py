"""Report pages shown as tabs on the reports page (besides the overview), one module each.

Each page gets the period, account and the one-off switch of the reports page's filter
bar through ``refresh(start, end, account_id, one_offs)``.
"""

from hiro_budget.gui.reports.base import ReportPage
from hiro_budget.gui.reports.budget_actual import BudgetActualReport
from hiro_budget.gui.reports.cash_flow import CashFlowReport
from hiro_budget.gui.reports.depot import DepotReport
from hiro_budget.gui.reports.mortgage import MortgageReport
from hiro_budget.gui.reports.net_worth import NetWorthReport
from hiro_budget.gui.reports.recurring import RecurringReport
from hiro_budget.gui.reports.savings_rate import SavingsRateReport
from hiro_budget.gui.reports.top_payees import TopPayeesReport
from hiro_budget.gui.reports.year_comparison import YearComparisonReport

__all__ = [
    "BudgetActualReport",
    "CashFlowReport",
    "DepotReport",
    "MortgageReport",
    "NetWorthReport",
    "RecurringReport",
    "ReportPage",
    "SavingsRateReport",
    "TopPayeesReport",
    "YearComparisonReport",
]
