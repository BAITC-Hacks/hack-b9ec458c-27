import calendar
from datetime import date

import pytest
from pydantic import ValidationError

from replenishment.demo import demo_dataset
from replenishment.engine import calculate_item, calculate_orders, isolated_excess, round_order
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


def test_regular_sales_change_forecast_and_order(item, policy):
    original = calculate_item(item, policy)
    for month in item.months:
        month.quantity *= 1.2
    for sale in item.sales:
        sale.quantity *= 1.2
    increased = calculate_item(item, policy)
    assert original.status == increased.status == "ok"
    assert increased.components["forecast"] > original.components["forecast"]
    assert increased.quantity > original.quantity


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


def _add_training_spike(item, day, customer_id, excess=5000):
    sale = next(s for s in item.sales if s.date == day)
    sale.customer_id = customer_id
    sale.quantity += excess
    next(m for m in item.months if m.month == day.replace(day=1)).quantity += excess


@pytest.mark.parametrize("dates", [
    (date(2026, 6, 12), date(2026, 8, 12)),
    (date(2026, 8, 7), date(2026, 8, 22)),
])
def test_multiple_distinct_customer_spikes_preserve_baseline(item, policy, dates):
    original = calculate_item(item, policy)
    for index, day in enumerate(dates):
        _add_training_spike(item, day, f"SYNTHETIC-ONE-OFF-{index}")
    result = calculate_item(item, policy)
    assert result.components["excluded_quantity"] == 10000
    assert result.components["forecast"] == pytest.approx(original.components["forecast"])
    assert result.quantity == original.quantity
    assert sum(month["excluded"] for month in result.history) == 10000


def test_same_customer_different_invoices_not_treated_as_one_off(item, policy):
    for day in (date(2026, 6, 12), date(2026, 8, 12)):
        _add_training_spike(item, day, "SYNTHETIC-REPEAT")
    excess, _, warnings = isolated_excess(item, policy)
    assert not excess
    assert any("повторный крупный клиент" in warning for warning in warnings)


@pytest.mark.parametrize("customer_id", [None, "", "   "])
def test_missing_customer_multiple_invoices_cannot_prove_isolation(item, policy, customer_id):
    for sale in item.sales:
        sale.customer_id = customer_id
    for day in (date(2026, 6, 12), date(2026, 8, 12)):
        _add_training_spike(item, day, customer_id)
    excess, _, warnings = isolated_excess(item, policy)
    assert not excess
    assert any("разные накладные" in warning for warning in warnings)


def test_missing_customer_split_invoices_aggregate_by_sku_day(policy):
    base, item = demo_dataset().items[:2]
    for sale in item.sales:
        sale.customer_id = None
    spike = next(s for s in item.sales if s.date == date(2026, 8, 12))
    spike.quantity /= 2
    item.sales.append(spike.model_copy(update={"document": "SYNTHETIC-OTHER-INVOICE"}))
    result = calculate_item(item, policy)
    assert result.components["excluded_quantity"] == 5000
    assert result.quantity == calculate_item(base, policy).quantity
    assert any("накладная не является ID клиента" in warning for warning in result.warnings)


def test_known_customer_with_unidentified_large_event_remains_ambiguous(item, policy):
    _add_training_spike(item, date(2026, 6, 12), "SYNTHETIC-ONE-OFF")
    _add_training_spike(item, date(2026, 8, 12), None)
    assert isolated_excess(item, policy)[0] == {}


def test_high_regime_across_distinct_customers_is_preserved(item, policy):
    for day in (7, 12, 17, 22):
        _add_training_spike(item, date(2026, 8, day), f"SYNTHETIC-CUSTOMER-{day}")
    assert isolated_excess(item, policy)[0] == {}


def test_repeat_customer_preserved_while_independent_one_off_is_removed(item, policy):
    for day in (date(2026, 6, 12), date(2026, 7, 12)):
        _add_training_spike(item, day, "SYNTHETIC-REPEATING")
    _add_training_spike(item, date(2026, 8, 12), "SYNTHETIC-ONE-OFF")
    assert isolated_excess(item, policy)[0] == {date(2026, 8, 1): 5000}


def test_multiple_spikes_reconcile_each_month_separately(item, policy):
    june, august = date(2026, 6, 12), date(2026, 8, 12)
    _add_training_spike(item, june, "SYNTHETIC-JUNE")
    _add_training_spike(item, august, "SYNTHETIC-AUGUST")
    item.months[-1].quantity += 1
    excess, _, warnings = isolated_excess(item, policy)
    assert excess == {june.replace(day=1): 5000}
    assert any("2026-08-01" in warning and "не сверяются" in warning for warning in warnings)


@pytest.mark.parametrize("refund, expected", [(-20, 5000), (-80, 0)])
def test_signed_returns_reconciled_before_spike_removal(policy, refund, expected):
    item = demo_dataset().items[1]
    sale = next(s for s in item.sales if s.date == date(2026, 8, 12))
    item.sales.append(sale.model_copy(update={"quantity": refund, "document": "SYNTHETIC-REFUND"}))
    item.months[-1].quantity += refund
    excess, _, warnings = isolated_excess(item, policy)
    assert sum(excess.values()) == expected
    if not expected:
        assert any("возвраты/корректировки" in warning for warning in warnings)


def test_insufficient_normal_baseline_preserves_peak(policy):
    item = demo_dataset().items[1]
    item.sales = [sale for sale in item.sales if sale.date >= date(2026, 8, 1)]
    excess, _, warnings = isolated_excess(item, policy)
    assert not excess
    assert any("Недостаточно обычных операций" in warning for warning in warnings)


def test_unresolved_high_event_at_history_boundary_is_preserved(item, policy):
    _add_training_spike(item, date(2026, 8, 27), "SYNTHETIC-LAST-EVENT")
    assert isolated_excess(item, policy)[0] == {}


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
