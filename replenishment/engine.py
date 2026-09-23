from __future__ import annotations

import calendar
import math
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING
from statistics import mean, median

from .models import Calculation, Dataset, Item, Policy, Recommendation


def shifted_month(month: date, delta: int) -> date:
    index = month.year * 12 + month.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


def round_order(quantity: float, minimum: float | None, multiple: float | None) -> float:
    if quantity <= 0:
        return 0.0
    amount = max(Decimal(str(quantity)), Decimal(str(minimum or 0)))
    if multiple:
        step = Decimal(str(multiple))
        amount = (amount / step).to_integral_value(rounding=ROUND_CEILING) * step
    return float(amount)


def isolated_excess(item: Item, policy: Policy):
    """Only isolated events; repeated high volumes remain in demand history.

    Group by anonymized customer/day if supplied, otherwise document/day.
    Compare with median and MAD, and retain the median normal portion of an event.
    Do not alter a monthly series when transaction coverage does not reconcile.
    """
    start = shifted_month(policy.as_of.replace(day=1), -policy.lookback_months)
    end = policy.as_of.replace(day=1)
    events, totals, refs = defaultdict(float), defaultdict(float), defaultdict(list)
    for sale in item.sales:
        if not start <= sale.date < end:
            continue
        month = sale.date.replace(day=1)
        totals[month] += sale.quantity
        if sale.quantity <= 0:
            continue
        identity = ("customer", sale.customer_id) if sale.customer_id else ("document", sale.document)
        key = sale.date, identity
        events[key] += sale.quantity
        refs[key].append(sale.source)
    if len(events) < 6:
        return {}, [], ["Мало операций для надёжного выделения разового всплеска."]
    center = median(events.values())
    mad = median(abs(q - center) for q in events.values())
    threshold = max(policy.outlier_multiplier * center, center + policy.mad_multiplier * mad)
    candidates = [key for key, qty in events.items() if qty > threshold]
    if len(candidates) != 1:
        return {}, [], (["Несколько крупных событий: сохранены, требуется проверка регулярности."] if candidates else [])
    key = candidates[0]
    month = key[0].replace(day=1)
    monthly = next((m.quantity for m in item.months if m.month == month), None)
    if monthly is None or not math.isclose(monthly, totals[month], rel_tol=1e-6, abs_tol=1e-6):
        return {}, [], ["Всплеск найден, но накладные не сверяются с месячным итогом: автоматическое исключение заблокировано."]
    excess = events[key] - center
    if monthly < excess:
        return {}, [], ["Возвраты/корректировки не позволяют однозначно исключить всплеск."]
    return {month: excess}, refs[key], [f"Исключена разовая часть операции {key[0]}: {excess:g}; обычная часть {center:g} сохранена."]


def calculate_item(item: Item, policy: Policy) -> Recommendation:
    row = Recommendation(key=item.key, supplier=item.supplier, sku=item.sku,
                         article=item.article, name=item.name, unit=item.unit,
                         category=item.category, status="needs_data", warnings=list(item.issues),
                         evidence=list(item.sources.values()), minimum=item.minimum, multiple=item.multiple)
    if item.invalid:
        row.status = "invalid_data"
        row.explanation = "Исправьте ошибки источников перед расчётом."
        return row
    missing = []
    stock_override = policy.stocks.get(item.key)
    stock = stock_override.quantity if stock_override else item.stock
    stock_date = stock_override.as_of if stock_override else item.stock_date
    if stock is None:
        missing.append("актуальный свободный остаток")
    elif stock_date != policy.as_of:
        missing.append("срез свободного остатка на дату расчёта")
    if not item.transit_known:
        missing.append("данные о товарах в пути")
    if not item.unit:
        missing.append("единица измерения")
    if item.unit_conversion_required:
        missing.append("подтверждённый перевод единиц закупки (бухты/метры)")
    if set(item.seasonality) != set(range(1, 13)) or any(not math.isfinite(s) or s <= 0 for s in item.seasonality.values()):
        missing.append("12 положительных коэффициентов сезонности")
    if any(s.arrival is None for s in item.shipments):
        missing.append("дата ожидаемого поступления")
    if any(s.arrival and s.arrival < policy.as_of for s in item.shipments):
        missing.append("статус просроченной поставки (не считать её уже поступившей)")
    if missing:
        row.explanation = "Нужны данные: " + "; ".join(missing) + "."
        return row

    cutoff = policy.as_of.replace(day=1)
    start = shifted_month(cutoff, -policy.lookback_months)
    months = sorted((m for m in item.months if start <= m.month < cutoff), key=lambda m: m.month)
    available = [m for m in months if m.quantity is not None]
    if len(available) < 3:
        row.explanation = "Нужно не менее трёх завершённых месяцев с известными продажами."
        return row
    if len(available) < len(months):
        row.warnings.append("Пустые месяцы не заменены нулями; оценка использует только известные месяцы.")
    row.warnings.append("Текущий незавершённый месяц исключён из обучения.")
    if not any(s.customer_id for s in item.sales):
        row.warnings.append("Нет обезличенного ID клиента: анализ возможен по накладным, не по клиенту.")
    if item.category is None:
        row.warnings.append("Категория отсутствует: категорийные правила не применялись.")
    if item.minimum is None and item.multiple is None:
        row.warnings.append("Условия MOQ/кратности неизвестны; проверьте количество перед утверждением.")

    excess, evidence, outlier_warnings = isolated_excess(item, policy)
    row.evidence.extend(evidence)
    row.warnings.extend(outlier_warnings)
    stockouts = policy.stockout_days.get(item.key, {})
    known_months = {m.month for m in available}
    if any(month not in known_months and days > 0 for month, days in stockouts.items()):
        row.explanation = "Подтверждённый stockout вне доступной истории продаж."
        return row
    raw_rates, clean_rates, corrected_rates = {}, {}, {}
    for m in available:
        days = calendar.monthrange(m.month.year, m.month.month)[1]
        season = item.seasonality[m.month.month]
        net = max(0.0, m.quantity)
        clean = max(0.0, net - excess.get(m.month, 0))
        raw_rates[m.month] = net / days / season
        clean_rates[m.month] = clean / days / season
        absent = stockouts.get(m.month, 0)
        if absent == days and net > 0:
            row.status = "invalid_data"
            row.explanation = "Полный месяц отсутствия товара противоречит положительным продажам; проверьте ввод stockout."
            return row
        if absent < days:
            corrected_rates[m.month] = clean / (days - absent) / season
        if m.quantity < 0:
            row.warnings.append(f"{m.month}: чистые продажи отрицательны; спрос принят 0, возвраты не превращены в продажи.")
        if m.opening_stock == 0 and not absent:
            row.warnings.append(f"{m.month}: нулевой исторический остаток — признак возможного stockout, дни не определены; компенсация не добавлена.")
        row.history.append({"month": str(m.month), "sales": m.quantity,
                            "excluded": excess.get(m.month, 0), "cleaned": clean,
                            "confirmed_stockout_days": absent, "opening_stock": m.opening_stock})
        row.evidence.append(m.source)
        if m.stock_source:
            row.evidence.append(m.stock_source)
    if not corrected_rates:
        row.explanation = "Нет наблюдаемого периода наличия товара для оценки упущенного спроса."
        return row
    for m in available:
        if m.month not in corrected_rates:
            corrected_rates[m.month] = median(corrected_rates.values())
            row.warnings.append(f"{m.month}: полный stockout, спрос оценён по доступным месяцам после снятия сезонности.")
    recent = sorted(corrected_rates)[-3:]
    if recent[-1] != shifted_month(cutoff, -1):
        row.warnings.append("Последний завершённый месяц не заполнен; база построена по более ранним известным месяцам, проверьте актуальность спроса.")
    base = mean(corrected_rates[m] for m in recent)
    clean_base = mean(clean_rates[m] for m in recent)
    raw_base = mean(raw_rates[m] for m in recent)

    # Annual growth: compare three corresponding months, never a seasonal peak vs a trough.
    all_months = {m.month: m for m in item.months if m.month < cutoff and m.quantity is not None}
    ratios = []
    for month in recent:
        prev = all_months.get(shifted_month(month, -12))
        if prev and prev.quantity > 0:
            rate = prev.quantity / calendar.monthrange(prev.month.year, prev.month.month)[1] / item.seasonality[prev.month.month]
            ratios.append(corrected_rates[month] / rate)
    trend = median(ratios) if len(ratios) == 3 and (all(x > 1 for x in ratios) or all(x < 1 for x in ratios)) else 1.0
    growth_source = "устойчивый годовой тренд" if trend != 1 else "устойчивый тренд не подтверждён"
    if item.growth_rate is not None and not excess:
        trend = 1 + item.growth_rate
        growth_source = "годовой коэффициент из переданного файла"
        row.warnings.append("Переданный коэффициент роста — входная оценка; не независимое доказательство устойчивого роста.")
    elif item.growth_rate is not None and excess:
        row.warnings.append("После исключения всплеска рост пересчитан по очищенной истории, чтобы исходный коэффициент не вернул выброс в прогноз.")
    if item.key in policy.growth_overrides:
        trend = 1 + policy.growth_overrides[item.key]
        growth_source = "явный сценарий менеджера"

    horizon = policy.category_horizons.get(item.category, policy.horizon_days)
    duration = horizon + policy.lead_days
    end = policy.as_of + timedelta(days=duration)
    future = [base * item.seasonality[(policy.as_of + timedelta(days=d)).month] * trend ** ((d + 1) / 365)
              for d in range(duration)]
    forecast = sum(future)
    inbound = sum(s.quantity for s in item.shipments if policy.as_of <= s.arrival < end)
    later = sum(s.quantity for s in item.shipments if s.arrival >= end)
    net_order = max(0.0, forecast - stock - inbound)
    # Whole pieces are indivisible. Other units retain numeric precision unless a multiple is supplied.
    multiple = item.multiple or (1.0 if item.unit.lower().startswith("шт") else None)
    quantity = round_order(net_order, item.minimum, multiple)
    balance = stock
    shortage = None
    for offset, demand in enumerate(future):
        day = policy.as_of + timedelta(days=offset)
        balance += sum(s.quantity for s in item.shipments if s.arrival == day)
        balance -= demand
        if balance < -1e-8 and shortage is None:
            shortage = day
    row.status = "ok"
    row.quantity, row.raw_quantity, row.multiple = quantity, net_order, multiple
    row.urgency = ("Дефицит до новой поставки" if shortage and shortage < policy.as_of + timedelta(days=policy.lead_days)
                   else "Пополнение в горизонте" if shortage else "Запаса достаточно")
    row.components = {
        "base_daily_deseasonalized": base, "raw_base_daily": raw_base,
        "clean_base_daily": clean_base, "stockout_daily_adjustment": base - clean_base,
        "excluded_quantity": sum(excess.values()), "annual_growth_factor": trend,
        "supplied_growth_rate": item.growth_rate,
        "growth_source": growth_source, "forecast": forecast, "stock": stock,
        "base_months": ", ".join(str(m) for m in recent),
        "stock_date": str(stock_date), "inbound_in_horizon": inbound,
        "inbound_after_horizon": later, "horizon_days": duration,
        "first_shortage": str(shortage) if shortage else None,
        "stock_source": "ввод менеджера" if stock_override else "переданный срез",
    }
    row.explanation = (f"Прогноз {forecast:.2f} {item.unit} на {duration} дней − свободный запас {stock:g} "
                       f"− поступления в горизонте {inbound:g} = {net_order:.2f}. "
                       f"После минимальной партии/кратности: {quantity:g}. "
                       f"База {base:.3f}/день без сезонности; сезонность применена по дням; "
                       f"годовой множитель роста {trend:.3f} ({growth_source}).")
    if later:
        row.warnings.append(f"За горизонтом поступит {later:g}; в вычет не включено.")
    if shortage and shortage < policy.as_of + timedelta(days=policy.lead_days):
        row.warnings.append("Обычная новая поставка не успевает к первому дефициту; требуется отдельное решение менеджера.")
    return row


def calculate_orders(dataset: Dataset, policy: Policy) -> Calculation:
    rows = []
    for item in dataset.items:
        if policy.suppliers and item.supplier not in policy.suppliers:
            continue
        if policy.categories and (item.category or "Без категории") not in policy.categories:
            continue
        rows.append(calculate_item(item, policy))
    return Calculation(rows=rows, issues=dataset.issues, synthetic=dataset.synthetic, policy=policy)
