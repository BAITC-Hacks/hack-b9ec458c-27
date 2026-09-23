"""Pure review/export: approval binds to the exact calculation and edited draft."""
import csv
import hashlib
import io
import json
import math

from .models import Calculation


def fingerprint(calculation: Calculation, quantities: dict[str, float], reason: str = "") -> str:
    validate_quantities(calculation, quantities)
    payload = {"calculation": calculation.model_dump(mode="json"), "quantities": quantities, "reason": reason}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_quantities(calculation: Calculation, quantities: dict[str, float]):
    rows = {r.key: r for r in calculation.rows if r.status == "ok"}
    if set(quantities) != set(rows):
        raise ValueError("Состав проекта заказа изменён. Выполните расчёт заново.")
    for key, qty in quantities.items():
        row = rows[key]
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty < 0:
            raise ValueError("Количество должно быть конечным неотрицательным числом.")
        if qty > 0 and row.minimum and qty < row.minimum:
            raise ValueError("Количество меньше минимальной партии.")
        if qty > 0 and row.multiple and not math.isclose(qty / row.multiple, round(qty / row.multiple), abs_tol=1e-7):
            raise ValueError("Количество не соответствует кратности.")


def safe_text(value):
    text = str(value if value is not None else "")
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def export_csv(calculation: Calculation, quantities: dict[str, float], approval: str | None, reason: str = "") -> bytes:
    current = fingerprint(calculation, quantities, reason)
    if not approval or current != approval:
        raise ValueError("Утвердите текущую версию заказа.")
    if any(q != r.quantity for r in calculation.rows if r.status == "ok" for q in [quantities[r.key]]) and not reason.strip():
        raise ValueError("Укажите причину корректировки.")
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Режим", "Дата расчёта", "Поставщик", "Код 1С", "Артикул", "Наименование", "Единица",
                     "Рекомендация", "Утверждено", "Срочность", "Обоснование", "Предупреждения", "Причина корректировки",
                     "Источники", "Период между закупками", "Срок поставки", "Версия"])
    for row in sorted(calculation.rows, key=lambda row: (row.supplier, row.sku)):
        if row.status != "ok" or quantities[row.key] <= 0:
            continue
        writer.writerow(["SYNTHETIC" if calculation.synthetic else "PARTNER DATA", str(calculation.policy.as_of),
                         safe_text(row.supplier), safe_text(row.sku), safe_text(row.article), safe_text(row.name), safe_text(row.unit),
                         row.quantity, quantities[row.key], row.urgency, safe_text(row.explanation),
                         safe_text(" | ".join(row.warnings)), safe_text(reason),
                         safe_text(" | ".join(f"{s.file}/{s.sheet}!{s.cell}" for s in row.evidence)),
                         calculation.policy.category_horizons.get(row.category, calculation.policy.horizon_days),
                         calculation.policy.lead_days, current])
    return output.getvalue().encode("utf-8-sig")
