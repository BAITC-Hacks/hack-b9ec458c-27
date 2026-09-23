"""Read-only rolling monthly forecast evaluation, never historical order simulation.

Each target is forecast with the same demand engine, using only earlier observations.
Source revisions are not timestamped, so this is not a point-in-time data archive.
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
from collections import Counter
from datetime import date
from pathlib import Path
from statistics import mean

from .engine import calculate_item, shifted_month
from .models import Dataset, Item, Month, Policy


def _known(month: Month | None) -> bool:
    return (month is not None and month.quantity is not None
            and math.isfinite(month.quantity) and month.quantity >= 0)


def _seasonality(training: dict[date, Month], cutoff: date) -> tuple[dict[int, float], str]:
    """Estimate daily-rate factors from two complete training-only annual cycles."""
    dates = [shifted_month(cutoff, -offset) for offset in range(1, 25)]
    neutral = {month: 1.0 for month in range(1, 13)}
    if not all(_known(training.get(month)) for month in dates):
        return neutral, "neutral_1_insufficient_24_consecutive_training_months"
    rates = {month: training[month].quantity / calendar.monthrange(month.year, month.month)[1]
             for month in dates}
    annual_mean = mean(rates.values())
    if annual_mean <= 0:
        return neutral, "neutral_1_zero_training_sales"
    factors = {month: mean(rate for day, rate in rates.items() if day.month == month) / annual_mean
               for month in range(1, 13)}
    if any(factor <= 0 for factor in factors.values()):
        # The demand engine requires positive factors; never fill zero months ad hoc.
        return neutral, "neutral_1_nonpositive_training_factor"
    return factors, "estimated_from_previous_24_training_months_daily_rates"


def _period(item: Item, target: date) -> dict:
    result = {"month": target.isoformat(), "status": "skipped"}
    if item.invalid:
        return {**result, "reason": "invalid_item"}
    if not item.unit:
        return {**result, "reason": "missing_unit"}

    # Do not validate or inspect later quantities while forecasting an earlier month.
    observed = [month for month in item.months if month.month <= target]
    dates = [month.month for month in observed]
    if any(month.day != 1 for month in dates) or len(dates) != len(set(dates)):
        return {**result, "reason": "invalid_or_duplicate_month"}
    records = {month.month: month for month in observed}
    actual = records.get(target)
    if actual is None or actual.quantity is None:
        return {**result, "reason": "missing_target_sales"}
    if not _known(actual):
        return {**result, "reason": "invalid_target_sales"}
    recent = [shifted_month(target, -offset) for offset in range(1, 4)]
    if any(records.get(month) is None or records[month].quantity is None for month in recent):
        return {**result, "reason": "missing_recent_training_month"}
    # 24 months cover the engine lookback, annual comparisons and seasonal estimation.
    start = shifted_month(target, -24)
    training = {day: month for day, month in records.items() if start <= day < target}
    if any(month.quantity is not None and not _known(month) for month in training.values()):
        return {**result, "reason": "invalid_training_sales"}
    sales = [sale.model_copy(deep=True) for sale in item.sales if start <= sale.date < target]
    if any(not math.isfinite(sale.quantity) for sale in sales):
        return {**result, "reason": "invalid_training_transaction"}
    factors, seasonality_method = _seasonality(training, target)

    # Stock/transit only satisfy the order engine's input gate. We consume forecast,
    # never its order quantity. They are not invented historical stock observations.
    replay = Item(
        supplier=item.supplier, sku=item.sku, article=item.article, name=item.name,
        unit=item.unit, category=None, stock=0, stock_date=target, transit_known=True,
        shipments=[], growth_rate=None, seasonality=factors, sales=sales,
        months=[month.model_copy(deep=True, update={"opening_stock": None, "stock_source": None})
                for month in training.values()],
    )
    days = calendar.monthrange(target.year, target.month)[1]
    row = calculate_item(replay, Policy(as_of=target, horizon_days=days, lead_days=0))
    if row.status != "ok":
        return {**result, "reason": "forecast_unavailable"}
    forecast = row.components["forecast"]
    previous = recent[0]
    baseline = records[previous].quantity / calendar.monthrange(previous.year, previous.month)[1] * days
    if not math.isfinite(forecast) or not math.isfinite(baseline):
        return {**result, "reason": "nonfinite_forecast"}
    known_dates = sorted(day for day, month in training.items() if _known(month))
    return {
        "month": target.isoformat(), "status": "ok", "actual_sales": actual.quantity,
        "forecast": forecast, "baseline_forecast": baseline,
        "absolute_error": abs(forecast - actual.quantity),
        "baseline_absolute_error": abs(baseline - actual.quantity),
        "train_start": known_dates[0].isoformat(), "train_end": known_dates[-1].isoformat(),
        "training_months": len(known_dates), "engine_lookback_months": 12,
        "seasonality_method": seasonality_method,
        "seasonality_factors": {str(month): factor for month, factor in factors.items()},
        "annual_growth_factor": row.components["annual_growth_factor"],
        "excluded_training_quantity": row.components["excluded_quantity"],
    }


def backtest_dataset(dataset: Dataset, months: int = 3, *, as_of: date | None = None) -> dict:
    """Compare forecasts with raw observed monthly sales, separately for every SKU.

    The last ``months`` calendar periods end at the latest available monthly record
    before ``as_of``'s month. Gaps are reported, never silently replaced with zeros.
    WAPE is a fraction (0.1 = 10%) and is undefined when actual sales total zero.
    """
    if isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= 36:
        raise ValueError("months must be an integer between 1 and 36")
    as_of = as_of or date.today()
    cutoff = as_of.replace(day=1)
    completed = [month.month for item in dataset.items for month in item.months
                 if month.month.day == 1 and month.month < cutoff]
    targets = ([shifted_month(max(completed), -offset) for offset in reversed(range(months))]
               if completed else [])
    items, all_skips = [], Counter()
    for item in dataset.items:
        periods = [_period(item, target) for target in targets]
        evaluated = [period for period in periods if period["status"] == "ok"]
        skips = Counter(period["reason"] for period in periods if period["status"] == "skipped")
        all_skips.update(skips)
        actual_total = sum(period["actual_sales"] for period in evaluated)
        errors = sum(period["absolute_error"] for period in evaluated)
        baseline_errors = sum(period["baseline_absolute_error"] for period in evaluated)
        items.append({
            "key": item.key, "supplier": item.supplier, "sku": item.sku,
            "article": item.article, "name": item.name, "unit": item.unit,
            "status": "evaluated" if evaluated else "skipped",
            "evaluated_periods": len(evaluated), "periods": periods,
            "skipped_reasons": dict(skips),
            "metrics": {
                "mae": errors / len(evaluated) if evaluated else None,
                "wape": errors / actual_total if actual_total else None,
                "baseline_mae": baseline_errors / len(evaluated) if evaluated else None,
                "baseline_wape": baseline_errors / actual_total if actual_total else None,
            },
        })
    return {
        "metadata": {
            "mode": "forecast_only", "target": "observed_monthly_sales",
            "synthetic": dataset.synthetic, "as_of": as_of.isoformat(),
            "requested_months": months, "target_months": [target.isoformat() for target in targets],
            "training_rule": "train_before_target", "baseline": "previous_month_daily_rate",
            "wape_scale": "fraction", "aggregation": "per_supplier_sku_unit_only",
            "ignored_current_inputs": ["stock", "transit", "growth_rate", "seasonality", "category", "moq"],
            "limitations": [
                "Compared with raw realized SALES, not unobservable demand or lost sales during stockout.",
                "Historical source revisions/availability are unknown: not a point-in-time archival replay.",
                "Current stock, transit, source growth and source seasonality are not historical inputs.",
                "Internal zero-stock/no-shipment adapter is forecast-only; it does not simulate historical orders.",
                "No historical stockout compensation is supplied; source monthly completeness is not independently verified.",
                "Seasonality needs 24 consecutive known training months; otherwise positive neutral factors are explicit.",
                "Metrics do not prove inventory savings, order accuracy, or production readiness.",
            ],
        },
        "coverage": {
            "items": len(items), "evaluated_items": sum(item["status"] == "evaluated" for item in items),
            "evaluated_periods": sum(item["evaluated_periods"] for item in items),
            "skipped_periods": sum(all_skips.values()), "skipped_reasons": dict(all_skips),
        },
        "items": items,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only monthly SALES forecast evaluation; no order submission.")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--months", type=int, default=3)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    if args.demo == bool(args.source):
        parser.error("Use either --demo or --source")
    if not 1 <= args.months <= 36:
        parser.error("--months must be between 1 and 36")
    from .demo import demo_dataset
    from .importer import load_dataset
    dataset = demo_dataset() if args.demo else load_dataset([Path(path) for path in args.source])
    print(json.dumps(backtest_dataset(dataset, args.months, as_of=args.as_of), ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
