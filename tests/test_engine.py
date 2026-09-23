import calendar
from datetime import date

import pytest
from pydantic import ValidationError

from replenishment.demo import demo_dataset
from replenishment.engine import calculate_item, calculate_orders, round_order
from replenishment.models import Policy, Shipment, StockInput


@pytest.fixture
def item():
    return demo_dataset().items[0]


@pytest.fixture
def policy():
    return Policy(as_of=date(2026, 9, 22), horizon_days=30, lead_days=7)


def test_isolated_spike_does_not_inflate_order(policy):
    base, spike = demo_dataset().items[:2]
    a, b = calculate_item(base, policy), calculate_item(spike, policy)
    assert a.status == b.status == "ok"
    assert b.components["excluded_quantity"] == 5000
    assert b.quantity == a.quantity
    assert b.components["forecast"] == pytest.approx(a.components["forecast"])


def test_source_stock_and_inbound_change_result(item, policy):
    original = calculate_item(item, policy)
    item.stock += 20
    more_stock = calculate_item(item, policy)
    assert more_stock.quantity < original.quantity
    item.shipments[0].quantity += 20
    assert calculate_item(item, policy).quantity < more_stock.quantity


def test_late_shipment_is_not_deducted(item, policy):
    before = calculate_item(item, policy)
    item.shipments[0].arrival = date(2026, 12, 1)
    after = calculate_item(item, policy)
    assert after.components["inbound_in_horizon"] == 0
    assert after.components["inbound_after_horizon"] == 10
    assert after.quantity > before.quantity


def test_seasonality_changes_future_forecast(item, policy):
    before = calculate_item(item, policy)
    item.seasonality[10] = 3
    assert calculate_item(item, policy).components["forecast"] > before.components["forecast"]


def test_sustained_growth_detected_separately(item, policy):
    for m in item.months:
        m.quantity = 10 * calendar.monthrange(m.month.year, m.month.month)[1] * item.seasonality[m.month.month] * (2 if m.month.year == 2026 else 1)
    item.sales = []
    assert calculate_item(item, policy).components["annual_growth_factor"] == pytest.approx(2)


def test_supplied_growth_is_not_silently_ignored(item, policy):
    a = calculate_item(item, policy)
    item.growth_rate = 0.5
    b = calculate_item(item, policy)
    assert b.components["forecast"] > a.components["forecast"]
    assert b.components["growth_source"] == "годовой коэффициент из переданного файла"


def test_confirmed_stockout_increases_estimate(item, policy):
    a = calculate_item(item, policy)
    policy.stockout_days = {item.key: {date(2026, 8, 1): 15}}
    b = calculate_item(item, policy)
    assert b.components["stockout_daily_adjustment"] > 0
    assert b.components["forecast"] > a.components["forecast"]


def test_zero_opening_stock_is_not_stockout_days(item, policy):
    a = calculate_item(item, policy)
    item.months[-1].opening_stock = 0
    b = calculate_item(item, policy)
    assert b.components["stockout_daily_adjustment"] == 0
    assert b.quantity == a.quantity
    assert any("дни не определены" in w for w in b.warnings)


def test_full_stockout_uses_observed_reference(item, policy):
    item.months[-1].quantity = 0
    policy.stockout_days = {item.key: {date(2026, 8, 1): 31}}
    row = calculate_item(item, policy)
    assert row.status == "ok"
    assert row.components["stockout_daily_adjustment"] > 0


def test_category_selection_and_explicit_category_policy(policy):
    d = demo_dataset()
    d.items[0].category = "A"
    policy.categories = ["A"]
    result = calculate_orders(d, policy)
    assert len(result.rows) == 1
    before = result.rows[0].quantity
    policy.category_horizons = {"A": 60}
    assert calculate_orders(d, policy).rows[0].quantity > before


@pytest.mark.parametrize("field,value", [("stock", None), ("unit", None), ("transit_known", False), ("seasonality", {})])
def test_missing_input_never_becomes_success(item, policy, field, value):
    setattr(item, field, value)
    result = calculate_item(item, policy)
    assert result.status == "needs_data"
    assert result.quantity is None


def test_stale_stock_not_current(item, policy):
    item.stock_date = date(2026, 9, 1)
    assert calculate_item(item, policy).status == "needs_data"
    policy.stocks = {item.key: StockInput(quantity=10, as_of=policy.as_of)}
    assert calculate_item(item, policy).status == "ok"


def test_invalid_source_blocks_calculation(item, policy):
    item.invalid = True
    assert calculate_item(item, policy).status == "invalid_data"


def test_piece_multiple_and_minimum_distinct():
    assert round_order(11, 20, 6) == 24
    assert round_order(0, 20, 6) == 0
    assert round_order(0.31, None, 0.1) == 0.4


def test_repeated_large_events_preserved(item, policy):
    for month in (6, 7, 8):
        sale = next(s for s in item.sales if s.date == date(2026, month, 12))
        sale.quantity += 5000
        next(m for m in item.months if m.month == date(2026, month, 1)).quantity += 5000
    row = calculate_item(item, policy)
    assert row.components["excluded_quantity"] == 0


def test_split_customer_purchase_grouped(policy):
    base, item = demo_dataset().items[:2]
    spike = next(s for s in item.sales if s.date == date(2026, 8, 12))
    spike.quantity = 1670
    item.sales.extend([spike.model_copy(update={"document": "split-2"}), spike.model_copy(update={"document": "split-3"})])
    assert calculate_item(item, policy).quantity == calculate_item(base, policy).quantity


def test_negative_sales_are_not_abs_values(item, policy):
    item.months[-1].quantity = -40
    row = calculate_item(item, policy)
    assert row.history[-1]["sales"] == -40
    assert row.history[-1]["cleaned"] == 0


def test_inconsistent_transactions_not_subtracted(policy):
    item = demo_dataset().items[1]
    item.months[-1].quantity += 1
    row = calculate_item(item, policy)
    assert row.components["excluded_quantity"] == 0
    assert any("не сверяются" in w for w in row.warnings)


def test_invalid_stockout_days_rejected():
    with pytest.raises(ValidationError):
        Policy(as_of=date(2026, 9, 22), horizon_days=30, lead_days=7,
               stockout_days={"X": {date(2026, 2, 1): 31}})


def test_no_mutation_of_input(item, policy):
    original = item.model_dump()
    calculate_item(item, policy)
    assert item.model_dump() == original


def test_source_growth_cannot_reintroduce_removed_spike(policy):
    base, spike = demo_dataset().items[:2]
    base.growth_rate = 0
    spike.growth_rate = 100  # Simulates a source formula distorted by the one-off sale.
    clean = calculate_item(base, policy)
    result = calculate_item(spike, policy)
    assert result.components["supplied_growth_rate"] == 100
    assert result.components["annual_growth_factor"] == pytest.approx(1)
    assert result.quantity == clean.quantity


def test_invalid_season_keys_return_needs_data(item, policy):
    item.seasonality = {m: 1.0 for m in range(12)}
    assert calculate_item(item, policy).status == "needs_data"
