"""Level clustering: a one-dimensional Gaussian mixture fitted by EM, sized by BIC.

Each segment found by the change-point detector contributes one point, its mean
log10-current. Points of the same operating state sit close together (their spread is the
estimation noise of a segment mean plus real variation), different states are far apart in
the log domain. For ``k = 1 .. k_max`` a mixture

    p(x) = sum_j w_j N(x; mu_j, s_j^2)

is fitted by expectation-maximisation, started from the k quantiles of the sorted points
(deterministic, so results are reproducible), with a variance floor ``s_min^2`` so that a
component cannot collapse onto a single point. The ``k`` with the lowest

    BIC = -2 ln L + (3k - 1) ln N

is kept, and each point is assigned to the component with the highest responsibility.
Components are returned ordered by mean (lowest current first).

With few segments (a short log with two or three wakes) BIC's penalty can outweigh the
likelihood gain of separating two clearly different levels. :func:`cluster_levels` therefore
first splits the sorted points wherever two neighbours are more than ``gap`` decades apart
(default 0.15, a factor of 1.41) and lets BIC decide only inside each group: well separated
levels are never merged, close ones are merged or split on the evidence.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

DEFAULT_MAX_STATES = 8
DEFAULT_GAP = 0.15  # decades
S_MIN = 0.02  # decades: two segments within ~5 % of each other are indistinguishable
_LOG_2PI = math.log(2.0 * math.pi)


@dataclass(frozen=True)
class Mixture:
    weights: list[float]
    means: list[float]
    sigmas: list[float]
    log_likelihood: float
    bic: float

    @property
    def k(self) -> int:
        return len(self.means)

    def log_pdf(self, j: int, x: float) -> float:
        s = self.sigmas[j]
        return -0.5 * (_LOG_2PI + 2.0 * math.log(s) + ((x - self.means[j]) / s) ** 2)

    def assign(self, x: float) -> int:
        scores = [math.log(self.weights[j]) + self.log_pdf(j, x) for j in range(self.k)]
        return max(range(self.k), key=lambda j: scores[j])


def _logsumexp(values: list[float]) -> float:
    m = max(values)
    if m == -math.inf:
        return m
    return m + math.log(sum(math.exp(v - m) for v in values))


def _bins(xs: list[float], width: float = 0.002) -> tuple[list[float], list[float]]:
    """Group points into bins of ``width`` decades: EM then runs on (bin mean, count) pairs."""
    groups: dict[int, list[float]] = {}
    for x in xs:
        groups.setdefault(round(x / width), []).append(x)
    keys = sorted(groups)
    return [sum(groups[g]) / len(groups[g]) for g in keys], [float(len(groups[g])) for g in keys]


def fit_gmm(
    xs: list[float], k: int, s_min: float = S_MIN, iterations: int = 300, tol: float = 1e-10
) -> Mixture:
    if k < 1 or len(xs) < k:
        raise ValueError("need at least k points")
    vals, counts = _bins(xs)
    n = float(len(xs))
    srt = sorted(xs)
    means = [srt[min(len(srt) - 1, int((j + 0.5) * len(srt) / k))] for j in range(k)]
    spread = (srt[-1] - srt[0]) / (2 * k) if srt[-1] > srt[0] else s_min
    sigmas = [max(spread, s_min)] * k
    weights = [1.0 / k] * k
    prev = -math.inf
    ll = -math.inf
    for _ in range(iterations):
        # E step (weighted by bin counts)
        resp: list[list[float]] = []
        ll = 0.0
        for x, c in zip(vals, counts, strict=False):
            terms = [
                math.log(weights[j])
                - 0.5 * (_LOG_2PI + 2.0 * math.log(sigmas[j]) + ((x - means[j]) / sigmas[j]) ** 2)
                for j in range(k)
            ]
            total = _logsumexp(terms)
            ll += c * total
            resp.append([c * math.exp(t - total) for t in terms])
        # M step
        for j in range(k):
            nj = sum(r[j] for r in resp)
            if nj < 1e-9:
                # An empty component: park it on the worst-explained point.
                worst = min(range(len(vals)), key=lambda i: max(resp[i]) / counts[i])
                means[j] = vals[worst]
                sigmas[j] = s_min
                weights[j] = 1.0 / n
                continue
            mu = sum(r[j] * x for r, x in zip(resp, vals, strict=False)) / nj
            var = sum(r[j] * (x - mu) ** 2 for r, x in zip(resp, vals, strict=False)) / nj
            means[j] = mu
            sigmas[j] = max(math.sqrt(var), s_min)
            weights[j] = nj / n
        total_w = sum(weights)
        weights = [w / total_w for w in weights]
        if abs(ll - prev) < tol * max(1.0, abs(ll)):
            break
        prev = ll
    order = sorted(range(k), key=lambda j: means[j])
    bic = -2.0 * ll + (3 * k - 1) * math.log(n)
    return Mixture(
        [weights[j] for j in order], [means[j] for j in order], [sigmas[j] for j in order], ll, bic
    )


def best_mixture(xs: list[float], max_k: int = DEFAULT_MAX_STATES, s_min: float = S_MIN) -> Mixture:
    """Fit k = 1..max_k components and keep the lowest BIC."""
    if not xs:
        raise ValueError("no points to cluster")
    distinct = len({round(x, 6) for x in xs})
    best: Mixture | None = None
    for k in range(1, min(max_k, distinct, len(xs)) + 1):
        m = fit_gmm(xs, k, s_min)
        if best is None or m.bic < best.bic:
            best = m
    assert best is not None
    return _drop_empty(best, xs)


def _drop_empty(m: Mixture, xs: list[float]) -> Mixture:
    """Remove components that no point is assigned to (they only shape the density)."""
    used = sorted({m.assign(x) for x in xs})
    if len(used) == m.k:
        return m
    w = [m.weights[j] for j in used]
    total = sum(w)
    return Mixture(
        [x / total for x in w],
        [m.means[j] for j in used],
        [m.sigmas[j] for j in used],
        m.log_likelihood,
        m.bic,
    )


def cluster_levels(
    xs: list[float], max_k: int = DEFAULT_MAX_STATES, gap: float = DEFAULT_GAP, s_min: float = S_MIN
) -> tuple[list[int], list[float]]:
    """Cluster points: returns a label per point and the cluster means (ascending).

    Groups separated by more than ``gap`` are fitted independently with :func:`best_mixture`;
    ``max_k`` limits the total number of clusters (if there are more well separated groups
    than that, the closest groups are joined first).
    """
    if not xs:
        raise ValueError("no points to cluster")
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    groups: list[list[int]] = [[order[0]]]
    for a, b in itertools.pairwise(order):
        if xs[b] - xs[a] > gap:
            groups.append([b])
        else:
            groups[-1].append(b)
    while len(groups) > max_k:
        # Join the two neighbouring groups with the smallest gap between them.
        gaps = [xs[groups[k + 1][0]] - xs[groups[k][-1]] for k in range(len(groups) - 1)]
        k = min(range(len(gaps)), key=lambda j: gaps[j])
        groups[k : k + 2] = [groups[k] + groups[k + 1]]
    budget = max_k - len(groups)  # extra clusters BIC may add inside groups
    labels = [0] * len(xs)
    means: list[float] = []
    for members in groups:
        pts = [xs[i] for i in members]
        mix = best_mixture(pts, 1 + budget, s_min)
        budget -= mix.k - 1
        base = len(means)
        for i, x in zip(members, pts, strict=False):
            labels[i] = base + mix.assign(x)
        means.extend(mix.means)
    return labels, means
