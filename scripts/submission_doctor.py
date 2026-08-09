#!/usr/bin/env python3
"""submission_doctor.py -- deterministic, zero-LLM validator/fixer for submission.json.

Usage: python3 scripts/submission_doctor.py <submission.json> [--fix <out.json>]
                [--template <template.json>] [--ledger <ledger.json>]
Exit 0 = submittable (no fatal issues), 1 = fatal issues remain.
"""
import argparse, json, math, re, sys

ROOT = __file__.rsplit("/scripts/", 1)[0]
DEF_TEMPLATE = ROOT + "/case-related-docs/submission_template.json"
DEF_LEDGER = ROOT + "/artifacts/ledger_real.json"
STATUSES = {"COMPLIANT", "BREACH"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("--fix", metavar="OUT")
    ap.add_argument("--template", default=DEF_TEMPLATE)
    ap.add_argument("--ledger", default=DEF_LEDGER)
    a = ap.parse_args()
    sub = json.load(open(a.submission))
    template = json.load(open(a.template))
    ledger = json.load(open(a.ledger))
    led_ids = {s: {r["txn_id"] for r in rows} for s, rows in ledger.items()}

    issues = []  # (level, message); level in OK-not-stored / FIXED-or-FIXABLE / FATAL / SKIP
    fixing = a.fix is not None
    FIX = "FIXED" if fixing else "FIXABLE"
    def rep(level, msg): issues.append((level, msg))

    ans = sub.get("answers")
    if not isinstance(ans, dict):
        rep("FATAL", "submission has no 'answers' dict"); ans = {}
    tans = template["answers"]

    for scen in tans:
        if scen not in ans:
            rep("FATAL", f"{scen}: scenario missing entirely")
    for scen in list(ans):
        if scen not in tans:
            rep(FIX, f"{scen}: extra scenario not in template -> dropped")
            if fixing: del ans[scen]

    ok_cells = 0
    for scen, tclauses in tans.items():
        cells = ans.get(scen)
        if not isinstance(cells, dict):
            continue  # already FATAL above (or wrong type)
        for cl in list(cells):
            if cl not in tclauses:
                rep(FIX, f"{scen}/{cl}: extra clause not in template -> dropped")
                if fixing: del cells[cl]
        for cl in tclauses:
            cell = cells.get(cl)
            if not isinstance(cell, dict):
                rep("FATAL", f"{scen}/{cl}: cell missing"); continue
            bad = False
            st = cell.get("status")
            if st not in STATUSES:
                rep("FATAL", f"{scen}/{cl}: status={st!r} (must be COMPLIANT|BREACH; cannot auto-fix)")
                bad = True
            act = cell.get("actual")
            if not isinstance(act, (int, float)) or isinstance(act, bool) \
                    or not math.isfinite(act) or act < 0:
                rep("FATAL", f"{scen}/{cl}: actual={act!r} (must be a non-negative number)")
                bad = True
            elif round(act, 2) != act:
                rep(FIX, f"{scen}/{cl}: actual {act} -> {round(act, 2)}")
                if fixing: cell["actual"] = round(act, 2)
            ev = cell.get("evidence_txn_id")
            if ev is not None and st == "COMPLIANT":
                rep(FIX, f"{scen}/{cl}: evidence {ev!r} on COMPLIANT -> null")
                if fixing: cell["evidence_txn_id"] = None
            elif ev is not None:
                if not isinstance(ev, str):
                    rep(FIX, f"{scen}/{cl}: evidence {ev!r} not a string -> null")
                    if fixing: cell["evidence_txn_id"] = None
                elif scen not in led_ids:
                    rep("SKIP", f"{scen}/{cl}: no ledger rows for scenario; evidence {ev!r} unverified")
                elif ev not in led_ids[scen]:
                    salv = next((t for t in re.findall(r"[A-Za-z0-9-]+", ev)
                                 if t in led_ids[scen]), None)
                    what = f"salvaged -> {salv!r}" if salv else "-> null"
                    rep(FIX, f"{scen}/{cl}: evidence {ev!r} not in {scen} ledger, {what}")
                    if fixing: cell["evidence_txn_id"] = salv
            if not bad:
                ok_cells += 1

    for lvl, msg in issues:
        print(f"{lvl:8s} {msg}")
    n_fatal = sum(1 for l, _ in issues if l == "FATAL")
    n_fix = sum(1 for l, _ in issues if l == FIX)
    n_skip = sum(1 for l, _ in issues if l == "SKIP")
    total = sum(len(v) for v in tans.values())
    print(f"SUMMARY  cells_ok={ok_cells}/{total} {FIX.lower()}={n_fix} "
          f"skipped={n_skip} FATAL={n_fatal}")
    if n_fatal:
        print(f"FATAL    {n_fatal} issue(s) CANNOT be auto-fixed -- DO NOT SUBMIT as-is")
    if fixing:
        json.dump(sub, open(a.fix, "w"), indent=2, ensure_ascii=False)
        print(f"WROTE    {a.fix}" + ("  (still contains FATAL cells!)" if n_fatal else ""))
    if not issues:
        print("OK       submission is clean")
    sys.exit(1 if n_fatal else 0)

if __name__ == "__main__":
    main()
