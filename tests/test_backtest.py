import calendar
import json
from datetime import date

import pytest

from replenishment.backtest import backtest_dataset
from replenishment.engine import shifted_month
from replenishment.models import Dataset, Item, Month, Sale, Shipment, SourceRef


REF = SourceRef(file="SYNTHETIC", sheet="backtest", cell="generated")


def dataset(count=30, daily=2):
    item = Item(supplier="DEMO", sku="TRAINING", unit="шт", name="Synthetic evaluation item")
    for offset in range(count):
        month = shifted_month(date(2024, 1, 1), offset)
        item.months.append(Month(month=month, quantity=daily * calendar.monthrange(month.year, month.month)[1], source=REF))
    return Dataset(items=[item], synthetic=True)


def result(data, months=3):
    return backtest_dataset(data, months, as_of=date(2026, 7, 15))


def test_observed_sales_comparison_and_fraction_metrics():
    data = dataset()
    data.items[0].months[-1].quantity = 90
    report = result(data, months=1)
    item = report["items"][0]
    period = item["periods"][0]
    assert period["month"] == "2026-06-01"
    assert period["actual_sales"] == 90
    assert period["forecast"] == pytest.approx(60)
    assert period["baseline_forecast"] == pytest.approx(60)
    assert item["metrics"]["mae"] == pytest.approx(30)
    assert item["metrics"]["wape"] == pytest.approx(1 / 3)
    assert report["metadata"]["target"] == "observed_monthly_sales"
    assert report["metadata"]["training_rule"] == "train_before_target"
    json.dumps(report, allow_nan=False)


def test_future_sales_and_targets_do_not_leak_into_prior_forecast():
    data = dataset()
    before = result(data)
    future = data.items[0].months[-1]
    future.quantity = 999999
    data.items[0].sales.append(Sale(date=date(2026, 6, 2), quantity=999999, document="future", warehouse="demo", source=REF))
    after = result(data)
    assert before["items"][0]["periods"][:2] == after["items"][0]["periods"][:2]
    assert before["items"][0]["periods"][-1]["forecast"] == after["items"][0]["periods"][-1]["forecast"]


def test_present_stock_transit_growth_seasonality_and_category_ignored():
    data = dataset()
    before = result(data)
    item = data.items[0]
    item.stock = 999999
    item.stock_date = date(2040, 1, 1)
    item.transit_known = True
    item.shipments = [Shipment(quantity=999999, arrival=date(2024, 1, 1), source=REF)]
    item.growth_rate = 99
    item.seasonality = {month: 9000 for month in range(1, 13)}
    item.category = "Future category"
    item.minimum = 88888
    item.multiple = 77777
    assert result(data) == before


def test_all_zero_actuals_make_wape_undefined_not_zero():
    data = dataset(daily=0)
    report = result(data)
    metrics = report["items"][0]["metrics"]
    assert metrics == {"mae": 0, "wape": None, "baseline_mae": 0, "baseline_wape": None}
    assert report["items"][0]["periods"][0]["seasonality_method"] == "neutral_1_zero_training_sales"


def test_seasonality_is_derived_only_from_two_training_cycles():
    data = dataset()
    for month in data.items[0].months:
        if month.month.month == 6:
            month.quantity *= 3
    report = result(data, months=1)
    period = report["items"][0]["periods"][0]
    assert period["seasonality_method"] == "estimated_from_previous_24_training_months_daily_rates"
    assert period["seasonality_factors"]["6"] / period["seasonality_factors"]["5"] == pytest.approx(3)
    assert period["forecast"] == pytest.approx(180)
    data.items[0].months[-1].quantity *= 100
    later = result(data, months=1)["items"][0]["periods"][0]
    assert later["seasonality_factors"] == period["seasonality_factors"]
    assert later["forecast"] == period["forecast"]


def test_short_history_uses_explicit_neutral_seasonality():
    report = result(dataset(count=8), months=1)
    period = report["items"][0]["periods"][0]
    assert period["seasonality_method"] == "neutral_1_insufficient_24_consecutive_training_months"
    assert set(period["seasonality_factors"].values()) == {1.0}


def test_input_is_not_mutated():
    data = dataset()
    before = data.model_dump()
    result(data)
    assert data.model_dump() == before


@pytest.mark.parametrize("change,reason", [
    ("missing_target", "missing_target_sales"), ("negative_target", "invalid_target_sales"),
    ("missing_recent", "missing_recent_training_month"), ("negative_train", "invalid_training_sales"),
    ("invalid_item", "invalid_item"), ("missing_unit", "missing_unit"),
    ("duplicate", "invalid_or_duplicate_month"),
])
def test_coverage_reports_skipped_periods(change, reason):
    data = dataset()
    item = data.items[0]
    if change == "missing_target":
        item.months[-1].quantity = None
    elif change == "negative_target":
        item.months[-1].quantity = -1
    elif change == "missing_recent":
        item.months.pop(-2)
    elif change == "negative_train":
        item.months[-2].quantity = -1
    elif change == "invalid_item":
        item.invalid = True
    elif change == "missing_unit":
        item.unit = None
    else:
        item.months.append(item.months[-2].model_copy(deep=True))
    report = result(data, months=1)
    assert report["coverage"]["evaluated_periods"] == 0
    assert report["coverage"]["skipped_reasons"] == {reason: 1}
    assert report["items"][0]["metrics"]["mae"] is None


def test_current_month_and_future_months_are_not_evaluation_targets():
    data = dataset(count=36)
    report = result(data)
    assert report["metadata"]["target_months"] == ["2026-04-01", "2026-05-01", "2026-06-01"]
    assert report["items"][0]["periods"][-1]["train_end"] == "2026-05-01"


def test_source_stock_only_month_is_not_evaluated_as_zero_sales():
    data = dataset()
    data.items[0].months.append(Month(month=date(2026, 7, 1), quantity=None, opening_stock=30, source=REF))
    report = backtest_dataset(data, 1, as_of=date(2026, 8, 1))
    assert report["coverage"]["skipped_reasons"] == {"missing_target_sales": 1}


def test_no_quantity_metrics_aggregated_across_units_or_skus():
    data = dataset()
    second = data.items[0].model_copy(deep=True, update={"sku": "OTHER", "unit": "метр"})
    second.months[-1].quantity *= 3
    data.items.append(second)
    report = result(data, months=1)
    assert len(report["items"]) == 2
    assert report["items"][0]["metrics"]["mae"] == pytest.approx(0)
    assert report["items"][1]["metrics"]["mae"] > 0
    assert "mae" not in report["coverage"]
    assert "metrics" not in report


def test_empty_dataset_reports_zero_coverage():
    report = result(Dataset(items=[]))
    assert report["coverage"]["evaluated_items"] == 0
    assert report["metadata"]["target_months"] == []
    assert report["items"] == []


@pytest.mark.parametrize("months", [0, -1, 37, True, 1.5])
def test_invalid_month_count_rejected(months):
    with pytest.raises(ValueError):
        result(dataset(), months=months)
