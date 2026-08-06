#!/usr/bin/env python3
"""S6 ASSEMBLE: fill submission_template.json from computed answers, validating
every rule that can zero a cell (CASE.ru.md §3).

Usage:
    python3 src/assemble.py artifacts/answers.json submission.json

answers.json shape: {"P1": {"6.1": {"status": ..., "actual": ..., "evidence_txn_id": ...}, ...}, ...}
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "case-related-docs" / "submission_template.json"

TEAM = "halykbank-ai"
CONTACT = "fikret.huseynov2006@gmail.com"
MODEL = "gpt-5"

# id shape derived from the actual ledger, not assumed
def _txn_re():
    import csv
    with open(REPO / "case-related-docs" / "master_ledger_2025.csv") as f:
        sample = next(csv.DictReader(f))["txn_id"]
    m = re.match(r"^([A-Za-z]+)-", sample)
    prefix = m.group(1) if m else "TXN"
    return re.compile(rf"^{prefix}-([A-Za-z]*\d+)-\d+$")

TXN_RE = _txn_re()


def validate_cell(sid: str, clause: str, cell: dict, errors: list) -> dict:
    where = f"{sid}/{clause}"
    status = cell.get("status")
    if status not in ("COMPLIANT", "BREACH"):
        errors.append(f"{where}: status {status!r} not COMPLIANT/BREACH")
    actual = cell.get("actual")
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        errors.append(f"{where}: actual {actual!r} is not a number")
    elif actual <= 0:
        errors.append(f"{where}: actual {actual} must be positive")
    else:
        actual = round(float(actual), 2)
    ev = cell.get("evidence_txn_id")
    if ev is not None:
        m = TXN_RE.match(str(ev))
        if not m:
            errors.append(f"{where}: evidence {ev!r} malformed")
        elif m.group(1) != sid:
            errors.append(f"{where}: evidence {ev!r} belongs to wrong scenario")
        if status == "COMPLIANT":
            errors.append(f"{where}: evidence set on a COMPLIANT cell (never true in GT)")
    return {"status": status, "actual": actual, "evidence_txn_id": ev}


def main() -> int:
    answers_path, out_path = sys.argv[1], sys.argv[2]
    answers = json.loads(Path(answers_path).read_text())
    template = json.loads(TEMPLATE.read_text())

    errors = []
    out = {"team": TEAM, "contact_email": CONTACT, "model": MODEL, "answers": {}}
    for sid, clauses in template["answers"].items():
        out["answers"][sid] = {}
        for clause in clauses:
            cell = answers.get(sid, {}).get(clause)
            if cell is None:
                errors.append(f"{sid}/{clause}: no computed answer (empty cell scores 0)")
                cell = {"status": None, "actual": None, "evidence_txn_id": None}
                out["answers"][sid][clause] = cell
            else:
                out["answers"][sid][clause] = validate_cell(sid, clause, cell, errors)

    # key parity: no extra scenarios/clauses in answers
    for sid, clauses in answers.items():
        if sid not in template["answers"]:
            errors.append(f"extra scenario {sid} not in template")
        else:
            for clause in clauses:
                if clause not in template["answers"][sid]:
                    errors.append(f"extra clause {sid}/{clause} not in template")

    Path(out_path).write_text(json.dumps(out, indent=2, ensure_ascii=False))
    json.loads(Path(out_path).read_text())  # round-trip: valid JSON

    if errors:
        print(f"WROTE {out_path} with {len(errors)} VALIDATION ERRORS:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"WROTE {out_path}: all cells valid, keys match template")
    return 0


if __name__ == "__main__":
    sys.exit(main())
