#!/usr/bin/env python3
"""Deterministic ledger prep: parse master_ledger_2025.csv, build the
scenario<->account map from txn-id prefixes, drop decoy rows.

Outputs:
    artifacts/ledger_real.json      {scenario_id: [row, ...]} (real rows only)
    artifacts/scenario_accounts.json {scenario_id: account_id}
"""
import csv
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
def _find_ledger():
    hits = sorted((REPO / "case-related-docs").glob("*.csv"))
    named = [h for h in hits if "ledger" in h.name.lower()]
    return (named or hits)[0]

LEDGER = _find_ledger()
TEMPLATE = REPO / "case-related-docs" / "submission_template.json"

def _txn_re():
    with open(LEDGER) as f:
        sample = next(csv.DictReader(f))["txn_id"]
    m = re.match(r"^([A-Za-z]+)-", sample)
    prefix = m.group(1) if m else "TXN"
    return re.compile(rf"^{prefix}-([A-Za-z0-9]+)-\d+$")

TXN_RE = _txn_re()


def load() -> tuple:
    scenario_ids = set(json.loads(TEMPLATE.read_text())["answers"].keys())
    rows_by_scenario = {sid: [] for sid in scenario_ids}
    accounts = {}
    n_total = n_decoy = 0
    with open(LEDGER) as f:
        for row in csv.DictReader(f):
            n_total += 1
            parts = row["txn_id"].split("-")
            sid = parts[1] if len(parts) >= 3 else None
            if sid not in scenario_ids:
                n_decoy += 1
                continue
            row["amount"] = float(row["amount"]) if row["amount"].strip() else None
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", row.get("date", "")):
                raise SystemExit(
                    f"NON-ISO DATE {row.get('date')!r} in {row['txn_id']}: "
                    "period filters compare dates as strings - normalize first")
            row.setdefault("currency", "USD")
            rows_by_scenario[sid].append(row)
            accounts.setdefault(sid, row.get("account_id", f"ACC-{sid}"))
    return rows_by_scenario, accounts, n_total, n_decoy


def main() -> None:
    rows, accounts, n_total, n_decoy = load()
    out_dir = REPO / "artifacts"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "ledger_real.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    (out_dir / "scenario_accounts.json").write_text(json.dumps(accounts, indent=1))
    missing = [
        (sid, r["txn_id"]) for sid in rows for r in rows[sid] if r["amount"] is None
    ]
    eur = [(sid, r["txn_id"]) for sid in rows for r in rows[sid] if r["currency"] != "USD"]
    print(f"total {n_total} rows: {n_total - n_decoy} real / {n_decoy} decoy")
    print(f"accounts: {accounts}")
    print(f"EMPTY-AMOUNT rows (values live in PDFs): {missing}")
    print(f"non-USD rows: {len(eur)}: {eur}")


if __name__ == "__main__":
    main()
