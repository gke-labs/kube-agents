"""Small-sample statistics for eval pass rates, in pure Python.

Every eval repetition is a Bernoulli trial: the deterministic checks either
all passed or they did not. Three repetitions is the presubmit's budget, so
the questions a developer asks -- did my change move this case, how many more
runs before I can tell, is it worth starting -- are small-sample questions,
and the normal approximation is wrong at these sizes (``scoring.py`` says as
much about its own aggregate floor). Everything here is exact:

* :func:`wilson_interval` -- the interval to print beside ``k/n``. Wilson
  rather than Wald because Wald collapses to a zero-width interval at 0/3
  and 3/3, exactly the two results a three-repetition run produces most.
* :func:`fisher_exact` -- two-sided Fisher exact test on a 2x2 table, the
  right test for "k of n on my branch against K of N on main" when n is 3
  and N is 800.
* :func:`reps_to_detect` -- the smallest n at which that test has the asked
  power against a given true rate. This is what ranks cases in ``bench-run
  plan``: a case whose rate on main is 0.87 needs more than twenty runs to
  show it moved to 1.0, while a case at 0.20 shows a move to 0.60 in ten.
* :func:`can_still_decide` -- whether any completion of a partly-run case
  could reach significance, so an adaptive run can stop early on futility
  and not just on success.

No scipy: the bench environment is what ``uv sync`` installs from
``pyproject.toml``, and a dependency for four functions is not worth the
resolver time on every CI run. Log-space hypergeometric terms via
:func:`math.lgamma` keep the sums finite at N in the hundreds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_POWER",
    "DEFAULT_Z",
    "Interval",
    "can_still_decide",
    "fisher_exact",
    "reps_to_detect",
    "wilson_interval",
]

# z for a 95% interval. The number every reader expects beside "95%".
DEFAULT_Z = 1.959964

# Significance level for the Fisher test and the planning power calculation.
# 0.05 two-sided, the convention; a developer who wants a stricter bar for a
# merge decision passes --alpha.
DEFAULT_ALPHA = 0.05

# Power target for reps_to_detect. 0.8 is the conventional planning figure
# and the one that keeps the recommended repetition counts in the range a
# laptop run can afford.
DEFAULT_POWER = 0.8

# Ceiling on the repetition counts reps_to_detect will search. Beyond this a
# case is not a laptop experiment, and the caller reports "more than N"
# rather than a number nobody will run.
MAX_PLANNED_REPS = 60

# Relative tolerance when comparing hypergeometric probabilities for the
# two-sided Fisher sum. Probabilities computed in log space differ from the
# observed table's by rounding; without a tolerance a table exactly as
# extreme as the observed one is sometimes dropped, which biases p low.
_FISHER_REL_TOL = 1e-7


@dataclass(frozen=True)
class Interval:
    """A confidence interval on a proportion, with the point estimate."""

    passes: int
    n: int
    low: float
    high: float

    @property
    def rate(self) -> float | None:
        return self.passes / self.n if self.n else None


def wilson_interval(passes: int, n: int, z: float = DEFAULT_Z) -> Interval:
    """Wilson score interval for ``passes`` successes in ``n`` trials.

    ``n == 0`` returns the whole unit interval: no data, no claim.
    """
    if n < 0 or passes < 0 or passes > n:
        raise ValueError(f"impossible count: {passes} passes of {n}")
    if n == 0:
        return Interval(passes=0, n=0, low=0.0, high=1.0)
    p = passes / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denominator
    half_width = (z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))) / denominator
    return Interval(
        passes=passes,
        n=n,
        low=max(0.0, centre - half_width),
        high=min(1.0, centre + half_width),
    )


def _log_hypergeom(a: int, row1: int, row2: int, col1: int) -> float:
    """log P(top-left cell == a) for fixed margins row1, row2, col1."""
    total = row1 + row2
    return (
        _log_comb(row1, a)
        + _log_comb(row2, col1 - a)
        - _log_comb(total, col1)
    )


def _log_comb(n: int, k: int) -> float:
    if k < 0 or k > n:
        return -math.inf
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact test p-value for the table ``[[a, b], [c, d]]``.

    Rows are the two groups (candidate, baseline); columns are pass and fail.
    The two-sided p-value sums every table with the same margins whose
    probability is at or below the observed table's, which is the definition
    R and scipy use.
    """
    for value in (a, b, c, d):
        if value < 0:
            raise ValueError("cell counts must be non-negative")
    row1, row2, col1 = a + b, c + d, a + c
    if row1 == 0 or row2 == 0:
        # One group has no observations: nothing to compare, nothing rejected.
        return 1.0
    observed = _log_hypergeom(a, row1, row2, col1)
    lowest = max(0, col1 - row2)
    highest = min(col1, row1)
    threshold = observed + math.log1p(_FISHER_REL_TOL)
    total = 0.0
    for x in range(lowest, highest + 1):
        log_p = _log_hypergeom(x, row1, row2, col1)
        if log_p <= threshold:
            total += math.exp(log_p)
    return min(1.0, total)


def _binomial_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    return math.exp(_log_comb(n, k) + k * math.log(p) + (n - k) * math.log1p(-p))


def power_at(
    n: int,
    true_rate: float,
    baseline_passes: int,
    baseline_n: int,
    alpha: float = DEFAULT_ALPHA,
) -> float:
    """Probability that ``n`` runs at ``true_rate`` reject equality with the baseline."""
    if n <= 0:
        return 0.0
    rejected = 0.0
    for k in range(n + 1):
        p_value = fisher_exact(k, n - k, baseline_passes, baseline_n - baseline_passes)
        if p_value < alpha:
            rejected += _binomial_pmf(k, n, true_rate)
    return rejected


def reps_to_detect(
    true_rate: float,
    baseline_passes: int,
    baseline_n: int,
    *,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
    max_reps: int = MAX_PLANNED_REPS,
) -> int | None:
    """Smallest ``n`` whose power against the baseline reaches ``power``.

    ``None`` when no ``n`` up to ``max_reps`` gets there -- the case's baseline
    is too close to ``true_rate``, or too thin, for a laptop run to settle.
    """
    if baseline_n <= 0:
        return None
    for n in range(1, max_reps + 1):
        if power_at(n, true_rate, baseline_passes, baseline_n, alpha) >= power:
            return n
    return None


def can_still_decide(
    passes: int,
    done: int,
    max_reps: int,
    baseline_passes: int,
    baseline_n: int,
    alpha: float = DEFAULT_ALPHA,
) -> bool:
    """Could any outcome of the remaining ``max_reps - done`` runs reach ``alpha``?

    Checks the two extreme completions -- every remaining run passes, every
    remaining run fails -- because the p-value is monotone in the direction of
    each. False means further runs cannot change the answer and an adaptive
    run should stop on futility.
    """
    remaining = max(0, max_reps - done)
    all_pass = fisher_exact(
        passes + remaining, done - passes, baseline_passes, baseline_n - baseline_passes
    )
    all_fail = fisher_exact(
        passes, done - passes + remaining, baseline_passes, baseline_n - baseline_passes
    )
    return min(all_pass, all_fail) < alpha
