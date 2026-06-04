"""Pure helpers for the Q6 mentions mispricing study.

The study asks whether the *edge is in the price*: how well does the market's own
pre-close price predict the realized outcome, is there a favorite-longshot bias, and
does the discredited GDELT "ubiquity" rate (``p_gdelt``) add anything over the price?

These functions do the math (numpy + scipy, no new dependencies); the CLI
(scripts/mentions_price_study.py) fetches candles and feeds rows in. Keeping them
pure makes the as-of price selection, the binning, and the hand-rolled logistic
regression unit-testable with inline data.
"""
from __future__ import annotations

import numpy

# Reliability bins (probability space) — the calibration table.
RELIABILITY_BINS: list[tuple[float, float]] = [
    (0.00, 0.15), (0.15, 0.50), (0.50, 0.85), (0.85, 1.01),
]
# Finer bins at the extremes to expose favorite-longshot bias.
FAVORITE_LONGSHOT_BINS: list[tuple[float, float]] = [
    (0.00, 0.05), (0.05, 0.15), (0.15, 0.50), (0.50, 0.85), (0.85, 0.95), (0.95, 1.01),
]


def select_asof_price(
    candles: list[dict],
    close_epoch_seconds: int,
    asof_minutes: int,
) -> dict | None:
    """Pick a representative market price as-of ``asof_minutes`` before close.

    ``candles`` are normalized dicts ``{"end_period_ts", "price_close", "price_mean",
    "volume"}`` with prices in **cents** (0–100); the CLI maps Kalshi's raw JSON into
    this shape. Selection, illiquidity-robust:

    1. latest candle ending at/before the as-of cutoff with a real **traded** close
       (non-null ``price_close`` and ``volume > 0``) → ``price_source="traded"``;
    2. else the latest such candle with a non-null ``price_mean`` (book mid, no trade)
       → ``price_source="mid_no_trade"``;
    3. else ``None`` — no usable price (the caller excludes the market from price metrics).

    Returns ``{"market_price", "price_source", "candle_volume", "candle_end_period_ts"}``
    with ``market_price`` as a probability in [0, 1], or ``None``.
    """
    cutoff_epoch_seconds = close_epoch_seconds - asof_minutes * 60
    in_window = [
        candle for candle in candles
        if candle.get("end_period_ts") is not None
        and candle["end_period_ts"] <= cutoff_epoch_seconds
    ]
    if not in_window:
        return None
    in_window.sort(key=lambda candle: candle["end_period_ts"])

    traded = [
        candle for candle in in_window
        if candle.get("price_close") is not None and (candle.get("volume") or 0) > 0
    ]
    if traded:
        chosen = traded[-1]
        price_cents = float(chosen["price_close"])
        price_source = "traded"
    else:
        with_mid = [candle for candle in in_window if candle.get("price_mean") is not None]
        if not with_mid:
            return None
        chosen = with_mid[-1]
        price_cents = float(chosen["price_mean"])
        price_source = "mid_no_trade"

    return {
        "market_price": max(0.0, min(1.0, price_cents / 100.0)),
        "price_source": price_source,
        "candle_volume": float(chosen.get("volume") or 0.0),
        "candle_end_period_ts": int(chosen["end_period_ts"]),
    }


def brier_score(rows: list[dict], prediction_key: str) -> float:
    """Mean squared error of ``row[prediction_key]`` vs ``row['realized_outcome']``."""
    if not rows:
        return float("nan")
    return float(
        numpy.mean([(float(row[prediction_key]) - row["realized_outcome"]) ** 2 for row in rows])
    )


def _bin_table(rows: list[dict], bins: list[tuple[float, float]], price_key: str) -> list[dict]:
    table: list[dict] = []
    for bin_low, bin_high in bins:
        bucket = [row for row in rows if bin_low <= float(row[price_key]) < bin_high]
        if not bucket:
            table.append({"bin": f"[{bin_low:.2f},{bin_high:.2f})", "count": 0,
                          "mean_price": None, "realized_yes_rate": None, "signed_bias": None})
            continue
        mean_price = float(numpy.mean([float(row[price_key]) for row in bucket]))
        realized_yes_rate = float(numpy.mean([row["realized_outcome"] for row in bucket]))
        table.append({
            "bin": f"[{bin_low:.2f},{bin_high:.2f})",
            "count": len(bucket),
            "mean_price": round(mean_price, 4),
            "realized_yes_rate": round(realized_yes_rate, 4),
            # Positive ⇒ realized more often than priced (longshots underpriced for YES).
            "signed_bias": round(realized_yes_rate - mean_price, 4),
        })
    return table


def reliability_table(rows: list[dict], price_key: str = "market_price") -> list[dict]:
    """Calibration table of price vs realized over the coarse reliability bins."""
    return _bin_table(rows, RELIABILITY_BINS, price_key)


def favorite_longshot_table(rows: list[dict], price_key: str = "market_price") -> list[dict]:
    """Calibration table over the fine extreme bins, to expose favorite-longshot bias."""
    return _bin_table(rows, FAVORITE_LONGSHOT_BINS, price_key)


# ---------------------------------------------------------------------------
# Hand-rolled logistic regression (no sklearn/statsmodels in the venv).
# ---------------------------------------------------------------------------

def standardize_features(feature_matrix: numpy.ndarray) -> tuple[numpy.ndarray, numpy.ndarray, numpy.ndarray]:
    """Return (standardized_matrix, column_means, column_standard_deviations)."""
    feature_matrix = numpy.atleast_2d(numpy.asarray(feature_matrix, dtype=float))
    column_means = feature_matrix.mean(axis=0)
    column_standard_deviations = feature_matrix.std(axis=0)
    column_standard_deviations[column_standard_deviations == 0] = 1.0
    standardized = (feature_matrix - column_means) / column_standard_deviations
    return standardized, column_means, column_standard_deviations


def fit_logistic_regression(
    feature_matrix: numpy.ndarray,
    outcomes: numpy.ndarray,
    *,
    max_iterations: int = 100,
    tolerance: float = 1e-9,
    ridge: float = 1e-6,
) -> numpy.ndarray:
    """Newton/IRLS logistic fit. Returns coefficients ``[intercept, b_1, ... b_k]``.

    ``feature_matrix`` is (n_samples, n_features) and should be standardized for
    interpretable, stable coefficients. A tiny ridge term keeps the Hessian invertible
    on near-separable toy data.
    """
    feature_matrix = numpy.atleast_2d(numpy.asarray(feature_matrix, dtype=float))
    outcomes = numpy.asarray(outcomes, dtype=float)
    design_matrix = numpy.column_stack([numpy.ones(len(outcomes)), feature_matrix])
    coefficients = numpy.zeros(design_matrix.shape[1])
    for _ in range(max_iterations):
        linear_predictor = design_matrix @ coefficients
        predicted = 1.0 / (1.0 + numpy.exp(-linear_predictor))
        predicted = numpy.clip(predicted, 1e-12, 1 - 1e-12)
        weights = predicted * (1.0 - predicted)
        gradient = design_matrix.T @ (outcomes - predicted)
        hessian = (design_matrix * weights[:, None]).T @ design_matrix
        hessian += ridge * numpy.eye(design_matrix.shape[1])
        step = numpy.linalg.solve(hessian, gradient)
        coefficients = coefficients + step
        if numpy.max(numpy.abs(step)) < tolerance:
            break
    return coefficients


def logistic_predicted_probabilities(
    feature_matrix: numpy.ndarray, coefficients: numpy.ndarray
) -> numpy.ndarray:
    feature_matrix = numpy.atleast_2d(numpy.asarray(feature_matrix, dtype=float))
    design_matrix = numpy.column_stack([numpy.ones(feature_matrix.shape[0]), feature_matrix])
    return 1.0 / (1.0 + numpy.exp(-(design_matrix @ coefficients)))


def logistic_log_likelihood(
    feature_matrix: numpy.ndarray, outcomes: numpy.ndarray, coefficients: numpy.ndarray
) -> float:
    outcomes = numpy.asarray(outcomes, dtype=float)
    predicted = numpy.clip(
        logistic_predicted_probabilities(feature_matrix, coefficients), 1e-12, 1 - 1e-12
    )
    return float(numpy.sum(outcomes * numpy.log(predicted) + (1 - outcomes) * numpy.log(1 - predicted)))


def bootstrap_coefficient_intervals(
    feature_matrix: numpy.ndarray,
    outcomes: numpy.ndarray,
    *,
    draws: int = 2000,
    seed: int = 12345,
) -> list[dict]:
    """Per-coefficient (intercept first) bootstrap 2.5/50/97.5 percentiles."""
    feature_matrix = numpy.atleast_2d(numpy.asarray(feature_matrix, dtype=float))
    outcomes = numpy.asarray(outcomes, dtype=float)
    sample_count = len(outcomes)
    random_generator = numpy.random.default_rng(seed)
    collected: list[numpy.ndarray] = []
    for _ in range(draws):
        resample_indices = random_generator.integers(0, sample_count, sample_count)
        try:
            coefficients = fit_logistic_regression(
                feature_matrix[resample_indices], outcomes[resample_indices]
            )
        except numpy.linalg.LinAlgError:
            continue
        collected.append(coefficients)
    if not collected:
        return []
    coefficient_draws = numpy.vstack(collected)
    intervals: list[dict] = []
    for coefficient_index in range(coefficient_draws.shape[1]):
        column = coefficient_draws[:, coefficient_index]
        intervals.append({
            "percentile_2_5": float(numpy.percentile(column, 2.5)),
            "percentile_50": float(numpy.percentile(column, 50)),
            "percentile_97_5": float(numpy.percentile(column, 97.5)),
        })
    return intervals


def likelihood_ratio_test(
    log_likelihood_full: float, log_likelihood_reduced: float, degrees_of_freedom: int
) -> float:
    """Two-model LR test p-value: does the larger model fit significantly better?"""
    from scipy.stats import chi2

    statistic = 2.0 * (log_likelihood_full - log_likelihood_reduced)
    if statistic <= 0:
        return 1.0
    return float(chi2.sf(statistic, degrees_of_freedom))


def pearson_correlation(first_values: list[float], second_values: list[float]) -> float:
    if len(first_values) < 2:
        return float("nan")
    return float(numpy.corrcoef(numpy.asarray(first_values, float), numpy.asarray(second_values, float))[0, 1])
