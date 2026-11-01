"""Categorization pipeline: transfers -> mortgage split -> rules -> ML -> uncategorized.

Bookings with a manual split are never touched. All other splits are recomputed from
scratch each time the pipeline runs on a booking.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.config import Settings
from hiro_budget.db.models import Account, Category, Split, SplitSource, Transaction
from hiro_budget.services.categorization.classifier import Classifier, features
from hiro_budget.services.categorization.mortgage import MortgageMatcher, extract_split
from hiro_budget.services.categorization.rules import compile_rules, first_match
from hiro_budget.services.categorization.transfers import (
    PatternMatcher,
    TransferTarget,
    find_transfer_target,
)
from hiro_budget.services.counterparts import sync_counterparts

log = logging.getLogger(__name__)


@dataclass
class CategorizationStats:
    counts: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(self.counts.items()))
        return parts or "nothing to do"


class Categorizer:
    """Holds compiled rules, matchers and the classifier for one session."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        # Splits reference the ORM objects (not ids) so relationships are usable before flush.
        self.categories: dict[str, Category] = {
            c.key: c for c in session.scalars(select(Category).where(Category.hidden.is_(False)))
        }
        accounts = {a.name: a for a in session.scalars(select(Account))}
        self.rules = compile_rules(settings.rules.rules)
        self.transfer_targets = [
            TransferTarget(
                accounts[a.name],
                PatternMatcher.compile(a.match),
                self.categories.get(a.transfer_category or ""),
            )
            for a in settings.app.own_accounts
            if a.match and a.name in accounts
        ]
        self.mortgages = [
            MortgageMatcher(
                name=m.name,
                matcher=PatternMatcher.compile(m.match),
                loan_account=accounts[m.loan_account],
                interest_category=self.categories[m.interest_category],
                principal_category=self.categories.get(m.principal_category or ""),
            )
            for m in settings.app.mortgage
            if m.loan_account in accounts and m.interest_category in self.categories
        ]
        ml = settings.app.ml
        self.classifier = Classifier.load(settings.model_path) if ml.enabled else None

    # -- single booking steps ----------------------------------------------------

    def _deterministic(self, tx: Transaction, stats: CategorizationStats) -> list[Split] | None:
        """Transfer, mortgage and rule steps. Returns splits or None if nothing matched."""
        target = find_transfer_target(self.transfer_targets, tx)
        if target is not None:
            stats.counts["transfer"] += 1
            # Money leaving the budget into a tracking account is budgeted like an expense.
            leaves_budget = (
                tx.amount_cents < 0 and tx.account.on_budget and not target.account.on_budget
            )
            return [
                Split(
                    amount_cents=tx.amount_cents,
                    transfer_account=target.account,
                    category=target.category if leaves_budget else None,
                    source=SplitSource.TRANSFER,
                )
            ]

        for m in self.mortgages:
            if not m.applies(tx):
                continue
            parts = extract_split(tx.purpose, tx.amount_cents)
            if parts is None:
                stats.warnings.append(
                    f"{tx.booking_date} {tx.amount_cents / 100:.2f}: mortgage '{m.name}' matched "
                    "but interest/principal could not be extracted or do not add up"
                )
                return None
            stats.counts["mortgage"] += 1
            return [
                Split(
                    amount_cents=parts.interest_cents,
                    category=m.interest_category,
                    source=SplitSource.MORTGAGE,
                    memo="Zinsen",
                ),
                Split(
                    amount_cents=parts.principal_cents,
                    transfer_account=m.loan_account,
                    # The loan is a tracking account: the principal leaves the budget
                    # and is budgeted in its own category.
                    category=m.principal_category,
                    source=SplitSource.MORTGAGE,
                    memo="Tilgung",
                ),
            ]

        rule = first_match(self.rules, tx)
        if rule is not None and rule.category in self.categories:
            stats.counts["rule"] += 1
            return [
                Split(
                    amount_cents=tx.amount_cents,
                    category=self.categories[rule.category],
                    source=SplitSource.RULE,
                    rule_id=rule.id,
                )
            ]
        return None

    # -- public API --------------------------------------------------------------

    def categorize(self, transactions: list[Transaction]) -> CategorizationStats:
        """(Re)compute splits for all non-manual bookings in ``transactions``."""
        stats = CategorizationStats()
        pending: list[Transaction] = []
        for tx in transactions:
            if tx.is_counterpart:
                continue  # managed by services.counterparts
            if tx.is_manual:
                stats.counts["manual (kept)"] += 1
                continue
            splits = self._deterministic(tx, stats)
            if splits is None:
                pending.append(tx)
            else:
                tx.splits[:] = splits

        predictions = (
            self.classifier.predict([features(t) for t in pending])
            if (self.classifier and pending)
            else []
        )
        threshold = self.settings.app.ml.min_confidence
        for i, tx in enumerate(pending):
            pred = predictions[i] if predictions else None
            if pred and pred.confidence >= threshold and pred.category_key in self.categories:
                stats.counts["ml"] += 1
                tx.splits[:] = [
                    Split(
                        amount_cents=tx.amount_cents,
                        category=self.categories[pred.category_key],
                        source=SplitSource.ML,
                        confidence=round(pred.confidence, 3),
                    )
                ]
            else:
                stats.counts["uncategorized"] += 1
                tx.splits[:] = [
                    Split(amount_cents=tx.amount_cents, source=SplitSource.UNCATEGORIZED)
                ]
        sync_counterparts(self.session, transactions, self.settings.app.matching.days)
        for w in stats.warnings:
            log.warning(w)
        return stats


def load_transactions(session: Session, ids: list[int] | None = None) -> list[Transaction]:
    q = select(Transaction).options(
        selectinload(Transaction.splits), selectinload(Transaction.account)
    )
    if ids is not None:
        q = q.where(Transaction.id.in_(ids))
    return list(session.scalars(q.order_by(Transaction.booking_date, Transaction.id)))


def training_samples(session: Session) -> list[tuple[str, str]]:
    """Bookings with exactly one manually or rule-assigned category split."""
    samples = []
    for tx in load_transactions(session):
        if len(tx.splits) != 1:
            continue
        s = tx.splits[0]
        if s.category is None or s.source not in (SplitSource.MANUAL, SplitSource.RULE):
            continue
        samples.append((features(tx), s.category.key))
    return samples


def train_classifier(session: Session, settings: Settings) -> Classifier | None:
    clf = Classifier.train(training_samples(session), settings.app.ml.min_samples)
    if clf is not None:
        clf.save(settings.model_path)
    return clf


def recategorize_all(session: Session, settings: Settings) -> CategorizationStats:
    return Categorizer(session, settings).categorize(load_transactions(session))
