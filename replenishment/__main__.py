import argparse
from collections import Counter
from datetime import date
from pathlib import Path

from .demo import demo_dataset
from .engine import calculate_orders
from .importer import load_dataset
from .models import Policy


def main():
    parser = argparse.ArgumentParser(description="Read-only calculation: no approval or submission.")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--horizon", type=int, required=True)
    parser.add_argument("--lead", type=int, required=True)
    args = parser.parse_args()
    if args.demo == bool(args.source):
        parser.error("Use either --demo or --source")
    data = demo_dataset() if args.demo else load_dataset([Path(p) for p in args.source])
    result = calculate_orders(data, Policy(as_of=args.as_of, horizon_days=args.horizon, lead_days=args.lead))
    print("MODE:", "SYNTHETIC" if data.synthetic else "PARTNER DATA")
    for source in data.sources:
        print(source.supplier, source.role, source.status, source.rows, source.message)
    print("ITEMS:", len(data.items))
    print("STATUS:", dict(Counter((r.supplier, r.status) for r in result.rows)))
    print("PROPOSED:", sum(r.status == "ok" and r.quantity > 0 for r in result.rows))
    print("MISSING:", Counter(r.explanation for r in result.rows if r.status != "ok").most_common(5))
    if data.synthetic:
        for row in result.rows:
            print(row.key, row.status, row.quantity, "excluded", row.components.get("excluded_quantity"))


if __name__ == "__main__":
    main()
