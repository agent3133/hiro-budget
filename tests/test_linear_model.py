"""The numpy classifier: n-grams as scikit-learn made them, training, saving, old models."""

import numpy as np

from hiro_budget.services.categorization.classifier import Classifier
from hiro_budget.services.categorization.linear_model import Tfidf, char_wb_ngrams

SAMPLES = [
    ("__out__ rewe markt lastschrift", "lebensmittel.supermarkt"),
    ("__out__ rewe city kartenzahlung", "lebensmittel.supermarkt"),
    ("__out__ aldi sued kartenzahlung", "lebensmittel.supermarkt"),
    ("__out__ shell tankstelle kartenzahlung", "mobilitaet.tanken"),
    ("__out__ aral station lastschrift", "mobilitaet.tanken"),
    ("__out__ esso tankstelle", "mobilitaet.tanken"),
    ("__in__ gehalt arbeitgeber gmbh", "einnahmen.gehalt"),
    ("__in__ lohn gehalt oktober", "einnahmen.gehalt"),
]


def test_ngrams_of_padded_words():
    assert char_wb_ngrams("ab", 3, 5) == [" ab", "ab ", " ab "]
    # a word shorter than n counts once: "a" padded is " a " (3), no 4- or 5-grams
    assert char_wb_ngrams("a", 3, 5) == [" a "]
    assert char_wb_ngrams("rewe", 3, 3) == [" re", "rew", "ewe", "we "]


def test_tfidf_rows_have_length_one():
    texts = [t for t, _ in SAMPLES]
    x = Tfidf.fit(texts).transform(texts)
    norms = np.sqrt(np.add.reduceat(x.data**2, x.indptr[:-1]))
    assert np.allclose(norms, 1)


def test_learns_and_predicts():
    clf = Classifier.train(SAMPLES, min_samples=5)
    preds = clf.predict(["__out__ rewe kartenzahlung", "__out__ shell", "__in__ gehalt"])
    assert [p.category_key for p in preds] == [
        "lebensmittel.supermarkt",
        "mobilitaet.tanken",
        "einnahmen.gehalt",
    ]
    assert all(0 < p.confidence <= 1 for p in preds)


def test_text_without_known_ngrams():
    clf = Classifier.train(SAMPLES, min_samples=5)
    (pred,) = clf.predict(["xq"])
    assert pred.confidence < 0.6  # no knowledge: no confident suggestion


def test_save_and_load(tmp_path):
    clf = Classifier.train(SAMPLES, min_samples=5)
    path = tmp_path / "data" / "classifier.npz"
    clf.save(path)
    loaded = Classifier.load(path)
    texts = ["__out__ aldi markt", "__in__ gehalt november"]
    for a, b in zip(clf.predict(texts), loaded.predict(texts), strict=True):
        assert a.category_key == b.category_key
        assert abs(a.confidence - b.confidence) < 1e-4
    assert loaded.n_samples == len(SAMPLES)


def test_unreadable_model_needs_retraining(tmp_path):
    path = tmp_path / "classifier.npz"
    path.write_bytes(b"not a model")
    assert Classifier.load(path) is None
