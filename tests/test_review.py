import csv
import io
from datetime import date

import pytest

from replenishment.demo import demo_dataset
from replenishment.engine import calculate_orders
from replenishment.models import Policy
from replenishment.review import export_csv, fingerprint


@pytest.fixture
def draft():
    result = calculate_orders(demo_dataset(), Policy(as_of=date(2026, 9, 22), horizon_days=30, lead_days=7))
    return result, {r.key: r.quantity for r in result.rows}


def test_no_export_before_approval(draft):
    c, q = draft
    with pytest.raises(ValueError, match="Утвердите"):
        export_csv(c, q, None)


def test_approved_export_is_repeatable_without_side_effect(draft):
    c, q = draft
    token = fingerprint(c, q)
    one, two = export_csv(c, q, token), export_csv(c, q, token)
    assert one == two
    rows = list(csv.DictReader(io.StringIO(one.decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 4
    assert all(r["Режим"] == "SYNTHETIC" for r in rows)


def test_quantity_change_invalidates_approval(draft):
    c, q = draft
    token = fingerprint(c, q)
    q[next(iter(q))] += 5
    with pytest.raises(ValueError, match="Утвердите"):
        export_csv(c, q, token, "ручная проверка")


def test_recalculation_invalidates_approval(draft):
    c, q = draft
    token = fingerprint(c, q)
    c.policy.horizon_days += 1
    with pytest.raises(ValueError):
        export_csv(c, q, token)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 1.5])
def test_invalid_edited_quantities_rejected(draft, value):
    c, q = draft
    q[next(iter(q))] = value
    with pytest.raises(ValueError):
        fingerprint(c, q)


def test_export_escapes_formula_injection(draft):
    c, q = draft
    c.rows[0].name = '=HYPERLINK("bad")'
    token = fingerprint(c, q)
    rows = list(csv.DictReader(io.StringIO(export_csv(c, q, token).decode("utf-8-sig")), delimiter=";"))
    assert rows[0]["Наименование"].startswith("'=")


def test_export_groups_suppliers_even_when_calculation_order_is_mixed(draft):
    calculation, quantities = draft
    calculation.rows = [calculation.rows[i] for i in (2, 0, 3, 1)]
    token = fingerprint(calculation, quantities)
    rows = list(csv.DictReader(io.StringIO(export_csv(calculation, quantities, token).decode("utf-8-sig")), delimiter=";"))
    assert [(row["Поставщик"], row["Код 1С"]) for row in rows] == sorted(
        (row["Поставщик"], row["Код 1С"]) for row in rows
    )


def test_export_records_effective_category_period():
    data = demo_dataset()
    data.items[0].category = "LONG"
    result = calculate_orders(data, Policy(as_of=date(2026, 9, 22), horizon_days=30, lead_days=7,
                                            category_horizons={"LONG": 60}))
    quantities = {r.key: r.quantity for r in result.rows}
    rows = list(csv.DictReader(io.StringIO(export_csv(
        result, quantities, fingerprint(result, quantities)).decode("utf-8-sig")), delimiter=";"))
    assert rows[0]["Период между закупками"] == "60"
    assert rows[0]["Срок поставки"] == "7"
    assert all(row["Период между закупками"] == "30" for row in rows[1:])
