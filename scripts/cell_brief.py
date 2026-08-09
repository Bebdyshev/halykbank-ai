#!/usr/bin/env python3
"""cell_brief.py SID CLAUSE — deterministic decision brief for one covenant cell.

Reads only artifacts/ (read-only). Zero LLM. Prints a compact brief so a human
can review/fix one cell in under a minute. Missing files/keys -> notes, no crash.
Set CELL_BRIEF_ART to point at a different artifacts directory (for testing).
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.environ.get("CELL_BRIEF_ART") or os.path.join(ROOT, "artifacts")

def load(name):
    try:
        with open(os.path.join(ART, name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"  [note] {name} missing (pipeline may still be running)")
    except Exception as e:  # noqa: BLE001
        print(f"  [note] {name} unreadable: {e}")
    return None

def hdr(t): print(f"\n=== {t} " + "=" * max(0, 66 - len(t)))
def money(x): return f"{x:,.2f}" if isinstance(x, (int, float)) else str(x)
def clip(s, n=300): s = " ".join(str(s).split()); return s if len(s) <= n else s[:n] + "…"

def th_str(th):
    if not isinstance(th, dict): return "?"
    return (f"{th.get('op', '?')} {money(th.get('value'))} "
            f"({'at-limit COMPLIES' if th.get('strict') else 'at-limit BREACHES'})")

def verdict(x, th):
    op, v, s = th.get("op"), th.get("value"), th.get("strict", True)
    if op not in ("<=", ">=") or not isinstance(v, (int, float)): return "?"
    ok = (x <= v if s else x < v) if op == "<=" else (x >= v if s else x > v)
    return "COMPLIANT" if ok else "BREACH"

def main():
    if len(sys.argv) != 3:
        sys.exit("usage: python3 scripts/cell_brief.py <SID> <CLAUSE>   e.g. H3 6.2")
    sid, clause = sys.argv[1], sys.argv[2]
    print(f"CELL BRIEF  {sid} / {clause}    (artifacts: {ART})")

    facts, trails = load("facts.json"), load("audit_trails.json")
    answers, judged = load("answers.json"), load("answers_judged.json")
    flags, ledger = load("flags.json"), load("ledger_real.json")

    hdr("1. CLAUSE SPEC (facts.json)")
    spec = None
    if facts is not None:
        clauses = (facts.get("covenants", {}).get(sid) or {}).get("clauses", [])
        spec = next((c for c in clauses if str(c.get("clause")) == clause), None)
        if spec is None:
            print(f"  [note] no spec for {sid}/{clause} "
                  f"(covenants present for: {sorted(facts.get('covenants', {})) or 'none'})")
    if spec:
        print(f"  title    : {spec.get('title')}\n  quote    : {clip(spec.get('quote'))}")
        print(f"  formula  : {spec.get('formula')}\n  threshold: {th_str(spec.get('threshold', {}))}")
        print(f"  condition: {spec.get('condition')}   period: {spec.get('period')}   "
              f"ebitda_addbacks: {spec.get('uses_ebitda_addbacks')}")
        for c in spec.get("components", []):
            d = c.get("definition", {})
            print(f"  comp {c.get('name')}: cats={d.get('categories')} "
                  f"subs_transfers={d.get('include_subsidiary_transfers')} period={d.get('period')}")
            print(f"       def_quote: {clip(d.get('definition_quote'), 180)}")
        if spec.get("unmodeled_condition"):
            print(f"  !! UNMODELED: {clip(spec['unmodeled_condition'])}")

    hdr("2. ENGINE TRAIL (audit_trails.json)")
    trail = (trails or {}).get(sid) or {}
    cell = (trail.get("cells") or {}).get(clause)
    if trails is not None and not cell:
        print(f"  [note] no trail cell for {sid}/{clause} "
              f"(cells present: {sorted(trail.get('cells') or {}) or 'none'})")
    if cell:
        print(f"  raw_metric: {cell.get('raw_metric')}   margin_pct: {cell.get('margin_pct')}")
        print(f"  threshold : {th_str(cell.get('threshold', {}))}")
        for k, v in sorted((cell.get("components") or {}).items()):
            print(f"  component {k} = {money(v)}")
        if cell.get("evidence_candidates"): print(f"  evidence_candidates: {cell['evidence_candidates']}")
        if cell.get("unmodeled_condition"): print(f"  !! UNMODELED: {clip(cell['unmodeled_condition'])}")

    hdr("3. ANSWER vs JUDGED")
    a = ((answers or {}).get(sid) or {}).get(clause)
    j = ((judged or {}).get(sid) or {}).get(clause)
    print(f"  engine: {a}\n  judged: {j}")
    if a and j:
        print("  " + ("!! DIFFER — judged overrode the engine, review why" if a != j else "identical"))

    hdr(f"4. FLAGS for {sid} (flags.json)")
    hits = [f for f in (flags or []) if isinstance(f, dict) and f.get("scenario") == sid]
    if flags is not None and not hits: print("  none")
    for f in hits:
        mark = ">>" if clause in str(f.get("detail", "")) else "  "
        print(f"  {mark} [{f.get('type')}] {clip(f.get('detail'), 200)}")

    hdr("5. PER-CATEGORY SUMS (ledger_real.json + trail facts)")
    rows, tf = (ledger or {}).get(sid), trail.get("facts") or {}
    cats, fills = tf.get("categories") or {}, tf.get("amount_fills") or {}
    excl, fx = set(tf.get("exclusions") or []), tf.get("fx_rates") or {}
    if ledger is not None and rows is None: print(f"  [note] no ledger rows for {sid}")
    if rows is not None and not cats: print("  [note] no categories in trail facts — sums unavailable")
    if rows and cats:
        sums, counts = {}, {}
        for r in rows:
            txn, cat = r["txn_id"], cats.get(r["txn_id"], "?UNCATEGORIZED")
            if cat == "NOISE" or txn in excl: continue
            amt = r["amount"] if r.get("amount") is not None else fills.get(txn)
            if amt is None: continue
            if r.get("currency", "USD") != "USD": amt *= fx.get(r["currency"]) or 0
            sums[cat] = sums.get(cat, 0.0) + abs(amt); counts[cat] = counts.get(cat, 0) + 1
        for cat in sorted(sums): print(f"  {cat:<18} {money(sums[cat]):>20}   rows={counts[cat]}")
        if excl: print(f"  (excluded txns: {sorted(excl)})")

    hdr("6. TOP-10 ROWS BY |AMOUNT|")
    if rows:
        eff = lambda r: r["amount"] if r.get("amount") is not None else fills.get(r["txn_id"], 0)
        for r in sorted(rows, key=lambda r: -abs(eff(r) or 0))[:10]:
            print(f"  {r['txn_id']} | {clip(r.get('counterparty'), 34):<34} | "
                  f"{clip(r.get('description'), 40):<40} | {money(eff(r)):>18} | "
                  f"{cats.get(r['txn_id'], '?')}")

    hdr("7. SANITY (recompute formula from trail components)")
    formula = (spec or {}).get("formula")
    comps = dict((cell or {}).get("components") or {})
    th = (cell or {}).get("threshold") or (spec or {}).get("threshold") or {}
    if not cell: print("  [note] no trail cell — nothing to recompute")
    elif not formula: print("  [note] no formula in facts.json — cannot recompute")
    else:
        try:
            val = eval(formula, {"__builtins__": {}, "max": max, "min": min}, comps)  # noqa: S307
            rep = cell.get("raw_metric")
            ok = isinstance(rep, (int, float)) and abs(val - rep) <= 1e-6 * max(1.0, abs(rep))
            print(f"  recomputed = {val}   reported raw_metric = {rep}   "
                  f"{'MATCH' if ok else '!! MISMATCH'}")
            v = verdict(val, th)
            print(f"  vs threshold {th_str(th)} -> {v}"
                  + (f"   (engine said {a['status']}"
                     + (")" if a.get("status") == v else " — !! DISAGREES)") if a else ""))
        except Exception as e:  # noqa: BLE001
            print(f"  [note] recompute failed: {e}")
    print()

if __name__ == "__main__":
    main()
