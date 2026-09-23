from pathlib import Path

import pytest
from openpyxl import Workbook

from replenishment.importer import load_dataset


def book(path, rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)
    wb.close()
    return path


def test_monthly_headers_and_leading_zero_code(tmp_path):
    path = book(tmp_path / "Ежемесячные продажи ИЭК.xlsx", [
        ["Номенклатура", "Номенклатура.Код", "янв. 2026", "февр. 2026", "март 2026"],
        [None, None, "Количество", "Количество", "Количество"],
        ["Товар", "001_", 10, None, 0],
    ])
    d = load_dataset([path])
    assert d.sources[0].status == "ok"
    assert d.items[0].sku == "001_"
    assert [m.quantity for m in d.items[0].months] == [10, None, 0]


def test_malformed_value_is_visible(tmp_path):
    p = book(tmp_path / "Ежемесячные продажи ИЭК.xlsx", [
        ["Номенклатура", "Номенклатура.Код", "янв. 2026"], ["Товар", "001", "banana"]])
    d = load_dataset([p])
    assert d.items[0].invalid
    assert d.items[0].months[0].quantity is None


def test_invalid_headers_reported(tmp_path):
    p = book(tmp_path / "Ежемесячные продажи ИЭК.xlsx", [["wrong"], [1]])
    assert load_dataset([p]).sources[0].status == "invalid_data"


def test_missing_folder_no_success(tmp_path):
    d = load_dataset([tmp_path / "absent"])
    assert not d.items
    assert any("недоступен" in s for s in d.issues)


def test_seasonality_empty_first_column(tmp_path):
    rows = [[None] for _ in range(10)]
    for month in ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"):
        rows.append([None, month] + [None] * 9 + [1.0])
    p = book(tmp_path / "Сезонность ИЭК.xlsx", rows)
    d = load_dataset([p])
    assert d.sources[0].status == "ok"
    assert d.sources[0].rows == 12


def test_opening_stock_never_becomes_current(tmp_path):
    p = book(tmp_path / "Ежемесячные остатки ИЭК.xlsx", [
        ["Номенклатура", "Ед.", "Номенклатура.Код", "сент. 2026"],
        ["Товар", "шт", "001", 12]])
    item = load_dataset([p]).items[0]
    assert item.stock is None
    assert item.months[0].opening_stock == 12


def test_transit_dates_not_just_total(tmp_path):
    p = book(tmp_path / "Путь ИЭК 22.09.2026.xlsx", [
        ["Код 1с", "Артикул ИЭК", "Наименование", "Поставка (поступление до 01.10.2026)", "Поставка (поступление до 01.12.2026)"],
        ["001", "ART", "Товар", 10, 20]])
    item = load_dataset([p]).items[0]
    assert item.transit_known
    assert [s.arrival.month for s in item.shipments] == [10, 12]
    assert [s.quantity for s in item.shipments] == [10, 20]


def test_duplicate_code_not_summed_silently(tmp_path):
    p = book(tmp_path / "MOQ ИЭК.xlsx", [["Код 1с", "Мин. разр. к отгр."], ["001", 2], ["001", 4]])
    assert load_dataset([p]).items[0].invalid


def test_minimum_and_multiple_distinguished(tmp_path):
    a = book(tmp_path / "MOQ ИЭК.xlsx", [["Код 1с", "Мин. разр. к отгр."], ["001", 20]])
    b = book(tmp_path / "MOQ SystemElectric.xlsx", [["Номенклатура.Код", "Кратность"], ["002", 6]])
    items = {i.supplier: i for i in load_dataset([a, b]).items}
    assert items["IEK"].minimum == 20 and items["IEK"].multiple is None
    assert items["Systeme Electric"].multiple == 6 and items["Systeme Electric"].minimum is None


def test_negative_operation_not_converted_to_positive(tmp_path):
    p = book(tmp_path / "Динамика продаж ИЭК.xlsx", [
        ["Дата", "Номер", "Документ", "Код", "Номенклатура", "Ед.", "Склад", "Количество"],
        ["01.08.2026 12:00:00", "001", "Расходная накладная 001", "001", "Товар", "шт", "Склад", -5],
        ["01.08.2026 12:00:00", "002", "Заказ покупателя 002", "001", "Товар", "шт", "Склад", 50]])
    item = load_dataset([p]).items[0]
    assert len(item.sales) == 1
    assert item.sales[0].quantity == -5


def test_missing_cached_formula_not_fabricated(tmp_path):
    p = book(tmp_path / "Ежемесячные продажи ИЭК.xlsx", [
        ["Номенклатура", "Номенклатура.Код", "янв. 2026"], ["Товар", "001", "=1+2"]])
    assert load_dataset([p]).items[0].months[0].quantity is None


def test_blank_transit_not_assumed_zero(tmp_path):
    p = book(tmp_path / "Путь ИЭК 22.09.2026.xlsx", [
        ["Код 1с", "Артикул ИЭК", "Наименование", "Поставка (поступление до 01.10.2026)"],
        ["001", "ART", "Товар", None]])
    assert not load_dataset([p]).items[0].transit_known
