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
LEDGER = REPO / "case-related-docs" / "master_ledger_2025.csv"
TEMPLATE = REPO / "case-related-docs" / "submission_template.json"

TXN_RE = re.compile(r"^TXN-([A-Za-z]+\d+)-\d+$")


def load() -> tuple:
    scenario_ids = set(json.loads(TEMPLATE.read_text())["answers"].keys())
    rows_by_scenario = {sid: [] for sid in scenario_ids}
    accounts = {}
    n_total = n_decoy = 0
    with open(LEDGER) as f:
        for row in csv.DictReader(f):
            n_total += 1
            m = TXN_RE.match(row["txn_id"])
            sid = m.group(1) if m else None
            if sid not in scenario_ids:
                n_decoy += 1
                continue
            row["amount"] = float(row["amount"]) if row["amount"].strip() else None
            rows_by_scenario[sid].append(row)
            accounts.setdefault(sid, row["account_id"])
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
