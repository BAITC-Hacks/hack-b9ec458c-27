"""Explicitly synthetic fixtures, never merged with partner data."""
import calendar
from datetime import date

from .models import Dataset, Item, Month, Sale, Shipment, SourceRef


def demo_dataset() -> Dataset:
    ref = SourceRef(file="SYNTHETIC", sheet="training", cell="generated")
    items = []
    for index, supplier in enumerate(("DEMO A", "DEMO B")):
        for spike in (False, True):
            sku = f"DEMO-{index}-{'SPIKE' if spike else 'REGULAR'}"
            item = Item(supplier=supplier, sku=sku, article=sku,
                        name="Учебный товар со всплеском" if spike else "Учебный регулярный товар",
                        unit="шт", category="DEMO", stock=5, stock_date=date(2026, 9, 22),
                        transit_known=True, multiple=5,
                        seasonality={m: (1.5 if m in (10, 11, 12) else 1.0) for m in range(1, 13)})
            for year in (2025, 2026):
                for month in range(1, 13 if year == 2025 else 9):
                    quantity = 0.0
                    for day in (2, 7, 12, 17, 22, 27):
                        q = 10.0 * item.seasonality[month]
                        if spike and year == 2026 and month == 8 and day == 12:
                            q += 5000
                        quantity += q
                        item.sales.append(Sale(date=date(year, month, day), quantity=q,
                                               document=f"SYN-{year}-{month}-{day}", warehouse="DEMO",
                                               customer_id=f"ANON-{day}", source=ref))
                    item.months.append(Month(month=date(year, month, 1), quantity=quantity, source=ref, opening_stock=100))
            item.shipments = [Shipment(quantity=10, arrival=date(2026, 9, 24), source=ref)]
            item.sources["stock"] = ref
            items.append(item)
    return Dataset(items=items, synthetic=True)
