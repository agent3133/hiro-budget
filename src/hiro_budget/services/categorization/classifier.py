"""Local ML classifier suggesting categories for bookings no rule matched.

TF-IDF over character n-grams of the booking text plus a direction token, fed into a
logistic regression (``linear_model``, numpy only). Trained on manually or
rule-categorized bookings.

numpy is imported only when a model is trained or loaded, not at the start of the app.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from hiro_budget.db.models import Transaction

if TYPE_CHECKING:
    import numpy as np

    from hiro_budget.services.categorization.linear_model import Tfidf

log = logging.getLogger(__name__)

FORMAT = 1  # of the saved model
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


def features(tx: Transaction) -> str:
    """Text representation used for training and prediction."""
    return feature_text(
        tx.amount_cents, tx.merchant, tx.counterparty, tx.booking_type, tx.purpose, tx.details
    )


def feature_text(amount_cents: int, *parts: str | None) -> str:
    """``features`` from plain column values (merchant, counterparty, booking type,
    purpose, details)."""
    direction = "__in__" if amount_cents > 0 else "__out__"
    text = " ".join(part for part in parts if part)
    # Digits (card numbers, references) are noise; collapse them.
    text = _SPACES.sub(" ", _DIGITS.sub("0", text.lower()))
    return f"{direction} {text}"


@dataclass
class Prediction:
    category_key: str
    confidence: float


class Classifier:
    def __init__(
        self,
        tfidf: Tfidf,
        weights: np.ndarray,
        intercept: np.ndarray,
        classes: list[str],
        n_samples: int,
        trained_at: datetime,
    ) -> None:
        self.tfidf = tfidf
        self.weights = weights
        self.intercept = intercept
        self.classes = classes
        self.n_samples = n_samples
        self.trained_at = trained_at

    @classmethod
    def train(cls, samples: list[tuple[str, str]], min_samples: int) -> Classifier | None:
        """Train on ``(feature_text, category_key)`` pairs; None if there is too little data."""
        labels = Counter(label for _, label in samples)
        if len(samples) < min_samples or len(labels) < 2:
            log.info(
                "not training classifier: %d samples, %d categories (need %d / 2)",
                len(samples),
                len(labels),
                min_samples,
            )
            return None
        import numpy as np

        from hiro_budget.services.categorization.linear_model import Tfidf, fit_logistic

        samples = sorted(samples)  # similar texts together: small blocks (linear_model)
        texts = [text for text, _ in samples]
        classes = sorted(labels)
        index = {c: i for i, c in enumerate(classes)}
        tfidf = Tfidf.fit(texts)
        y = np.array([index[label] for _, label in samples])
        weights, intercept = fit_logistic(tfidf.transform(texts), y, len(classes))
        log.info("classifier trained on %d samples, %d categories", len(samples), len(labels))
        return cls(tfidf, weights, intercept, classes, len(samples), datetime.now())

    def predict(self, texts: list[str]) -> list[Prediction]:
        if not texts:
            return []
        from hiro_budget.services.categorization.linear_model import softmax

        probas = softmax(self.tfidf.transform(texts).dot(self.weights) + self.intercept)
        best = probas.argmax(axis=1)
        return [
            Prediction(self.classes[i], float(row[i])) for i, row in zip(best, probas, strict=True)
        ]

    def save(self, path: Path) -> None:
        """numpy arrays and the vocabulary in one file (no pickle: safe to load)."""
        import numpy as np

        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "format": FORMAT,
            "n_samples": self.n_samples,
            "trained_at": self.trained_at.isoformat(),
            "classes": self.classes,
        }
        terms = sorted(self.tfidf.vocabulary, key=self.tfidf.vocabulary.__getitem__)
        # written next to it and moved over it: training runs in a worker thread (#55),
        # and a Categorizer loading the model meanwhile must never see half a file
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("wb") as f:  # a file object: savez would append ".npz" to a name
            np.savez_compressed(
                f,
                meta=np.array(json.dumps(meta)),
                terms=np.array(terms),
                idf=self.tfidf.idf,
                weights=self.weights.astype(np.float32),
                intercept=self.intercept,
            )
        for attempt in range(20):  # Windows refuses while a reader has the file open
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)

    @classmethod
    def load(cls, path: Path) -> Classifier | None:
        if not path.exists():
            return None
        import numpy as np

        from hiro_budget.services.categorization.linear_model import Tfidf

        try:
            with np.load(path, allow_pickle=False) as data:
                meta = json.loads(str(data["meta"]))
                if meta["format"] != FORMAT:
                    raise ValueError(f"format {meta['format']}")
                terms = [str(t) for t in data["terms"]]
                tfidf = Tfidf({t: i for i, t in enumerate(terms)}, data["idf"])
                weights = data["weights"].astype(float)
                intercept = data["intercept"]
            return cls(
                tfidf,
                weights,
                intercept,
                meta["classes"],
                meta["n_samples"],
                datetime.fromisoformat(meta["trained_at"]),
            )
        except Exception as e:  # an old scikit-learn model, a corrupt file, ...
            log.warning("cannot load classifier %s (%s); retrain needed", path, e)
            return None
