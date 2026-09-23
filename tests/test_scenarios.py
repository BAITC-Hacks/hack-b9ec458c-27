from datetime import date

import pytest

from replenishment.engine import calculate_orders
from replenishment.models import Policy
from replenishment.scenarios import SCENARIOS, training_scenario


@pytest.mark.parametrize("name", SCENARIOS)
def test_training_scenarios_are_synthetic_and_calculable(name):
    data, stockouts, description = training_scenario(name)
    assert data.synthetic and description
    assert all(ref.file == "SYNTHETIC" for i in data.items for ref in i.sources.values())
    result = calculate_orders(data, Policy(as_of=date(2026, 9, 22), horizon_days=30,
                                          lead_days=7, stockout_days=stockouts))
    assert all(r.status == "ok" and r.explanation for r in result.rows)
    first, second = result.rows[:2]
    if name in SCENARIOS[:3]:
        assert first.quantity == second.quantity
        assert second.components["excluded_quantity"] == (8000 if name == SCENARIOS[2] else 5000)
    elif name == SCENARIOS[3]:
        assert second.components["stockout_daily_adjustment"] > 0
        assert second.quantity > first.quantity
        assert not any(s.date.year == 2026 and (
            s.date.month == 7 and 6 <= s.date.day <= 15 or
            s.date.month == 8 and 5 <= s.date.day <= 19) for s in data.items[1].sales)
    elif name == SCENARIOS[4]:
        assert second.components["annual_growth_factor"] > first.components["annual_growth_factor"]
        assert second.quantity > first.quantity
    else:
        assert second.quantity < first.quantity


def test_scenarios_are_independent_and_unknown_name_rejected():
    first, _, _ = training_scenario(SCENARIOS[0])
    first.items[0].stock = 999
    second, _, _ = training_scenario(SCENARIOS[0])
    assert second.items[0].stock == 5
    with pytest.raises(ValueError):
        training_scenario("partner")
