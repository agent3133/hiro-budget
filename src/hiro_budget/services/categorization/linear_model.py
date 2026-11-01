"""TF-IDF over character n-grams and a multinomial logistic regression, in numpy only.

The same model scikit-learn built before (``TfidfVectorizer(analyzer="char_wb",
ngram_range=(3, 5), sublinear_tf=True)`` and ``LogisticRegression(C=10)``), without
scikit-learn and scipy: the app only needs this one model, and the two libraries are most
of the built exe.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np

NGRAMS = (3, 5)
C = 10.0  # inverse strength of the L2 penalty, as in scikit-learn
MAX_ITER = 2000
GTOL = 1e-4  # stop when no gradient component is larger (scikit-learn's lbfgs default)
ROWS_PER_BLOCK = 256  # of the training matrix, see Blocks


def char_wb_ngrams(text: str, lo: int = NGRAMS[0], hi: int = NGRAMS[1]) -> list[str]:
    """The n-grams of each word padded with a space, as scikit-learn's "char_wb"."""
    grams: list[str] = []
    for word in text.split():
        w = f" {word} "
        for n in range(lo, hi + 1):
            offset = 0
            grams.append(w[offset : offset + n])
            while offset + n < len(w):
                offset += 1
                grams.append(w[offset : offset + n])
            if offset == 0:  # a word shorter than n counts once
                break
    return grams


class Sparse:
    """Rows of a sparse matrix (CSR): ``data[indptr[i]:indptr[i + 1]]`` at the columns in
    ``indices``. Predictions multiply it directly; training goes through ``Blocks``."""

    def __init__(self, indptr: np.ndarray, indices: np.ndarray, data: np.ndarray, cols: int):
        self.indptr, self.indices, self.data, self.cols = indptr, indices, data, cols
        self.rows = np.repeat(np.arange(len(indptr) - 1), np.diff(indptr))

    @property
    def n(self) -> int:
        return len(self.indptr) - 1

    def dot(self, w: np.ndarray) -> np.ndarray:
        """``X @ w`` (a row without entries, e.g. a text of unknown n-grams, stays 0)."""
        out = np.zeros((self.n, w.shape[1]))
        used = np.diff(self.indptr) > 0
        if used.any():
            products = self.data[:, None] * w[self.indices]
            out[used] = np.add.reduceat(products, self.indptr[:-1][used], axis=0)
        return out


class Blocks:
    """The training matrix as dense float32 pieces of ``rows`` rows, each over only the
    columns its rows use: the products run in BLAS, but in a fraction of the memory of the
    whole matrix as one dense array (ten years of bookings: 26 instead of 323 MB), since
    rows of similar texts next to each other share most n-grams (``Classifier.train``
    sorts them). Plain numpy sums of the sparse entries were 10× slower."""

    def __init__(self, x: Sparse, rows: int = ROWS_PER_BLOCK) -> None:
        self.n, self.cols = x.n, x.cols
        self.blocks: list[tuple[int, int, np.ndarray, np.ndarray]] = []
        for r0 in range(0, x.n, rows):
            r1 = min(r0 + rows, x.n)
            start, end = x.indptr[r0], x.indptr[r1]
            cols, local = np.unique(x.indices[start:end], return_inverse=True)
            dense = np.zeros((r1 - r0, len(cols)), dtype=np.float32)
            dense[x.rows[start:end] - r0, local] = x.data[start:end]
            self.blocks.append((r0, r1, cols, dense))

    def dot(self, w: np.ndarray) -> np.ndarray:
        """``X @ w``."""
        w32 = w.astype(np.float32)
        out = np.empty((self.n, w.shape[1]))
        for r0, r1, cols, dense in self.blocks:
            out[r0:r1] = dense @ w32[cols]
        return out

    def tdot(self, g: np.ndarray) -> np.ndarray:
        """``X.T @ g``."""
        g32 = g.astype(np.float32)
        out = np.zeros((self.cols, g.shape[1]), dtype=np.float32)
        for r0, r1, cols, dense in self.blocks:
            out[cols] += dense.T @ g32[r0:r1]
        return out.astype(float)


class Tfidf:
    """Vocabulary and inverse document frequencies learnt from the training texts."""

    def __init__(self, vocabulary: dict[str, int], idf: np.ndarray) -> None:
        self.vocabulary = vocabulary
        self.idf = idf

    @classmethod
    def fit(cls, texts: list[str]) -> Tfidf:
        df: Counter[str] = Counter()
        for text in texts:
            df.update(set(char_wb_ngrams(text)))
        terms = sorted(df)
        counts = np.array([df[t] for t in terms], dtype=float)
        idf = np.log((1 + len(texts)) / (1 + counts)) + 1  # smoothed, as scikit-learn
        return cls({t: i for i, t in enumerate(terms)}, idf)

    def transform(self, texts: list[str]) -> Sparse:
        """Sublinear tf (1 + ln tf) times idf, each row scaled to length 1; n-grams not
        seen in training are left out."""
        indptr, indices, data = [0], [], []
        for text in texts:
            tf = Counter(g for g in char_wb_ngrams(text) if g in self.vocabulary)
            cols = [self.vocabulary[g] for g in tf]
            values = np.array([1 + math.log(c) for c in tf.values()]) * self.idf[cols]
            norm = float(np.sqrt((values**2).sum())) if len(values) else 0.0
            indices.extend(cols)
            data.extend((values / norm) if norm else values)
            indptr.append(len(indices))
        return Sparse(
            np.array(indptr), np.array(indices, dtype=np.int64), np.array(data), len(self.idf)
        )


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fit_logistic(sparse: Sparse, y: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Weights (features × classes) and intercepts minimising the mean cross-entropy plus
    ``|W|² / (2 C n)`` (scikit-learn's objective, scaled by 1/n); the intercept is not
    penalised."""
    x = Blocks(sparse)
    n, f = x.n, x.cols
    onehot = np.zeros((n, k))
    onehot[np.arange(n), y] = 1
    alpha = 1 / (C * n)

    def loss_grad(theta: np.ndarray) -> tuple[float, np.ndarray]:
        w, b = theta[: f * k].reshape(f, k), theta[f * k :]
        z = x.dot(w) + b
        z_max = z.max(axis=1, keepdims=True)
        log_sum = np.log(np.exp(z - z_max).sum(axis=1)) + z_max[:, 0]
        loss = (log_sum - z[np.arange(n), y]).mean() + 0.5 * alpha * (w * w).sum()
        g = (softmax(z) - onehot) / n
        grad_w = x.tdot(g) + alpha * w
        return loss, np.concatenate((grad_w.ravel(), g.sum(axis=0)))

    theta = lbfgs(loss_grad, np.zeros(f * k + k))
    return theta[: f * k].reshape(f, k), theta[f * k :]


def lbfgs(fun, x0: np.ndarray, m: int = 10) -> np.ndarray:
    """Limited-memory BFGS with a backtracking (Armijo) line search."""
    x = x0
    f, g = fun(x)
    s_hist: list[np.ndarray] = []
    y_hist: list[np.ndarray] = []
    for _ in range(MAX_ITER):
        if np.abs(g).max() <= GTOL:
            break
        # two-loop recursion: d = -H g
        q = g.copy()
        alphas = []
        for s, yv in zip(reversed(s_hist), reversed(y_hist), strict=True):
            a = (s @ q) / (yv @ s)
            alphas.append(a)
            q -= a * yv
        if s_hist:
            q *= (s_hist[-1] @ y_hist[-1]) / (y_hist[-1] @ y_hist[-1])
        for (s, yv), a in zip(zip(s_hist, y_hist, strict=True), reversed(alphas), strict=True):
            q += s * (a - (yv @ q) / (yv @ s))
        d = -q
        slope = g @ d
        if slope >= 0:  # not a descent direction: start over from the gradient
            s_hist.clear()
            y_hist.clear()
            d, slope = -g, -(g @ g)
        step = 1.0 if s_hist else min(1.0, 1 / np.abs(g).sum())
        while True:
            x_new = x + step * d
            f_new, g_new = fun(x_new)
            if f_new <= f + 1e-4 * step * slope or step < 1e-12:
                break
            step *= 0.5
        s, yv = x_new - x, g_new - g
        if s @ yv > 1e-10:  # keep the pair only if the curvature is positive
            s_hist.append(s)
            y_hist.append(yv)
            if len(s_hist) > m:
                s_hist.pop(0)
                y_hist.pop(0)
        if f - f_new <= 1e-12 * max(1.0, abs(f)):
            x, f, g = x_new, f_new, g_new
            break
        x, f, g = x_new, f_new, g_new
    return x
