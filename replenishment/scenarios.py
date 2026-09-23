"""Wholly synthetic scenarios. Never overlay them onto a partner dataset."""
from datetime import date

from .demo import demo_dataset
from .models import Dataset, SourceRef

SCENARIOS = (
    "Базовый пример",
    "Один клиент — несколько накладных",
    "Несколько разовых клиентов",
    "Сценарные периоды stockout",
    "Сезонность и устойчивый рост",
    "Изменение остатка и товаров в пути",
)


def training_scenario(name: str) -> tuple[Dataset, dict, str]:
    if name not in SCENARIOS:
        raise ValueError("Unknown training scenario")
    data = demo_dataset()
    stockouts = {}
    description = "Пара REGULAR/SPIKE: добавленная разовая часть 5000 не должна увеличивать регулярный заказ."
    if name == SCENARIOS[0]:
        return data, stockouts, description
    regular, spike = data.items[:2]
    data.items = [regular, spike]
    if name == SCENARIOS[1]:
        sale = next(s for s in spike.sales if s.date == date(2026, 8, 12))
        sale.customer_id = "SYNTHETIC-CUSTOMER-ONE"
        sale.quantity = 1670
        spike.sales.extend([sale.model_copy(update={"document": "SYN-SPLIT-2"}),
                            sale.model_copy(update={"document": "SYN-SPLIT-3"})])
        description = ("Вымышленный клиент SYNTHETIC-CUSTOMER-ONE купил 5010 шт. 12.08.2026 "
                       "по трём разным накладным. Из них 5000 — разовая часть, 10 — обычная. "
                       "ID клиента задан отдельно от номера накладной.")
    elif name == SCENARIOS[2]:
        first = next(s for s in spike.sales if s.date == date(2026, 8, 12))
        first.customer_id = "SYNTHETIC-ONE-OFF-A"
        second = next(s for s in spike.sales if s.date == date(2026, 7, 12))
        second.customer_id = "SYNTHETIC-ONE-OFF-B"
        second.quantity += 3000
        next(m for m in spike.months if m.month == date(2026, 7, 1)).quantity += 3000
        description = ("Два разных вымышленных клиента: разовые части 3000 шт. 12.07.2026 "
                       "и 5000 шт. 12.08.2026. Регулярные продажи между событиями сохранены.")
    else:
        changed = regular.model_copy(deep=True)
        changed.sku = changed.article = "DEMO-SCENARIO"
        changed.name = name
        data.items = [regular, changed]
        if name == SCENARIOS[3]:
            stockouts = {changed.key: {date(2026, 7, 1): 10, date(2026, 8, 1): 15}}
            # Keep the same monthly totals, but no synthetic sale on an absent day.
            moved_days = {7: {7: 16, 12: 18}, 8: {7: 20, 12: 21, 17: 23}}
            for sale in changed.sales:
                if sale.date.year == 2026:
                    day = moved_days.get(sale.date.month, {}).get(sale.date.day)
                    if day:
                        sale.date = sale.date.replace(day=day)
            description = ("СЦЕНАРНОЕ допущение только для DEMO-SCENARIO: отсутствие товара "
                           "06–15 июля (10 дней) и 05–19 августа 2026 (15 дней). "
                           "У обеих позиций одинаковые фактические учебные продажи; "
                           "компенсация упущенного спроса применяется только к DEMO-SCENARIO. "
                           "Эти периоды не получены из помесячных остатков партнёра.")
        elif name == SCENARIOS[4]:
            # Preserve the seasonal pattern and introduce sustained year-on-year growth.
            for month in changed.months:
                if month.month.year == 2026:
                    month.quantity *= 1.5
            for sale in changed.sales:
                if sale.date.year == 2026:
                    sale.quantity *= 1.5
            description = ("Учебная сезонность: октябрь–декабрь ×1,5. У DEMO-SCENARIO "
                           "продажи 2026 года дополнительно выше на 50%, чем у контрольной позиции. "
                           "Рост определяется из истории, не выдумывается моделью.")
        else:
            changed.stock += 20
            changed.shipments[0].quantity += 20
            description = ("При одинаковых учебных продажах DEMO-SCENARIO имеет на 20 шт. "
                           "больше свободного запаса и на 20 шт. больше в пути. "
                           "Ожидаем уменьшение рекомендуемого заказа.")
    for item in data.items:
        item.sources["scenario"] = SourceRef(file="SYNTHETIC", sheet=name, cell="scenario")
    return data, stockouts, description
