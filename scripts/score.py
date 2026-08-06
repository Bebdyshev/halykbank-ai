#!/usr/bin/env python3
"""Replica of the official Halyk AI Challenge scoring formula.

Usage:
    python3 scripts/score.py <submission.json> [--key <ground_truth.json>] [--quiet]

Per-cell scoring (see CASE.ru.md §4):
    status   0.50  exact match "COMPLIANT"/"BREACH"; wrong status zeroes the WHOLE cell
    actual   0.30  linear decay with relative error, zero at 5%:
                   0.30 * max(0, 1 - e/0.05), e = |sub - key| / |key|
    evidence 0.20  if key is a txn id: exact match or 0
                   if key is null: 0.20 rides the same actual-accuracy scale

The official total is difficulty-weighted per cell; the weights are not
published, so this reports the unweighted sum (max 36.0) plus a per-cell table.
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_KEY = REPO / "case-related-docs" / "ground_truth.json"


def actual_scale(sub_actual, key_actual) -> float:
    if not isinstance(sub_actual, (int, float)) or isinstance(sub_actual, bool):
        return 0.0
    if key_actual == 0:
        return 1.0 if sub_actual == 0 else 0.0
    e = abs(sub_actual - key_actual) / abs(key_actual)
    return max(0.0, 1.0 - e / 0.05)


def score_cell(sub: dict, key: dict) -> dict:
    out = {"status": 0.0, "actual": 0.0, "evidence": 0.0, "notes": []}
    if not isinstance(sub, dict):
        out["notes"].append("cell missing/invalid")
        return out

    if sub.get("status") != key["status"]:
        out["notes"].append(f"status {sub.get('status')!r} != {key['status']!r} -> cell zeroed")
        return out
    out["status"] = 0.5

    scale = actual_scale(sub.get("actual"), key["actual"])
    out["actual"] = 0.3 * scale
    if scale < 1.0:
        out["notes"].append(f"actual {sub.get('actual')} vs {key['actual']} (scale {scale:.3f})")

    if key["evidence_txn_id"] is not None:
        if sub.get("evidence_txn_id") == key["evidence_txn_id"]:
            out["evidence"] = 0.2
        else:
            out["notes"].append(
                f"evidence {sub.get('evidence_txn_id')!r} != {key['evidence_txn_id']!r}"
            )
    else:
        out["evidence"] = 0.2 * scale
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("--key", default=str(DEFAULT_KEY))
    ap.add_argument("--quiet", action="store_true", help="print only the total")
    args = ap.parse_args()

    sub = json.loads(Path(args.submission).read_text())
    key = json.loads(Path(args.key).read_text())

    answers = sub.get("answers", {})
    total = 0.0
    n_cells = 0
    rows = []
    for sid, scen in sorted(key["scenarios"].items()):
        for clause, kcell in sorted(scen["covenants"].items()):
            n_cells += 1
            scell = answers.get(sid, {}).get(clause)
            r = score_cell(scell, kcell)
            cell_total = r["status"] + r["actual"] + r["evidence"]
            total += cell_total
            rows.append((sid, clause, cell_total, r))

    if not args.quiet:
        for sid, clause, cell_total, r in rows:
            mark = "OK " if cell_total >= 0.999 else ("--- " if cell_total == 0 else "PART")
            line = f"{mark} {sid:>4} {clause}  {cell_total:.3f}  (s={r['status']:.2f} a={r['actual']:.2f} e={r['evidence']:.2f})"
            if r["notes"]:
                line += "  | " + "; ".join(r["notes"])
            print(line)
        print("-" * 60)
    print(f"TOTAL: {total:.3f} / {n_cells}.0  ({total / n_cells * 100:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
