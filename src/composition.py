#!/usr/bin/env python3
"""Composition map: which ledger rows does THIS borrower's auditor include in
each covenant-formula component?

The loan agreements are templated, but the financial-statement notes come from
five different audit firms, each defining «Выручка» / «Операционные расходы» /
EBITDA in its own way. A fixed category taxonomy cannot capture that, so for
every scenario one strong call reads: the covenant clause quotes + the FULL
auditor notes + the categorized ledger, and returns an EXPLICIT txn-id list per
component, each grounded in a note quote.

Deterministic reconciliation: when the auditor STATES an aggregate figure
(stated_figures from the audit extraction), the composition's sum for the
matching component must equal it to the cent - one repair call otherwise,
then a flag.

Output: artifacts/composition.json
    {scenario: {clause: {component: {"txn_ids": [...], "off_ledger_ids": [...],
                                     "basis_quote": ...}}}}
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import STRONG, generate, pmap  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ART = REPO / "artifacts"
PARSED = ART / "parsed_docs"

SCHEMA = {
    "type": "object",
    "properties": {
        "clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause": {"type": "string"},
                    "components": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "txn_ids": {"type": "array", "items": {"type": "string"}},
                                "off_ledger_ids": {
                                    "type": "array", "items": {"type": "string"},
                                    "description": "ids of off-ledger facts that belong in this component"},
                                "basis_quote": {"type": "string"},
                                "confident": {"type": "boolean",
                                              "description": "false when the notes leave the composition ambiguous"},
                            },
                            "required": ["name", "txn_ids", "off_ledger_ids",
                                         "basis_quote", "confident"],
                        },
                    },
                },
                "required": ["clause", "components"],
            },
        },
    },
    "required": ["clauses"],
}

PROMPT = """The borrower's loan covenants reference line items («Выручка», «Операционные
расходы», EBITDA...) AS DEFINED BY ITS AUDITOR's financial-statement notes. Different
audit firms compose these items differently. Your job: for EVERY component of EVERY
covenant formula below, list the EXACT ledger transactions this auditor's framework
includes, grounded in the notes.

COVENANT CLAUSES AND THEIR MACHINE SPECS (component names + our default category guess):
{clauses}

AUDITOR'S NOTES (full text - authoritative for what counts in each line item):
{notes}

ADJUSTMENT FACTS already applied by the engine (do NOT re-apply amounts; just decide
membership; off-ledger items may be included by their ids):
{adjustments}

LEDGER (txn | date | counterparty | description | amount | our category label):
{rows}

Rules:
- decide membership per the AUDITOR's framework, not our category labels; labels are
  hints that may be wrong;
- decoy rows (incoherent counterparty/description or generic recycled names) belong to
  NO component;
- a transaction excluded by the auditor (cutoff) still must NOT be listed;
- reclassified transactions belong to the component matching their POST-reclassification
  nature;
- ratios' numerators/denominators must not silently share rows unless the definitions say so;
- when the notes state an aggregate figure for an item, your list MUST sum to it
  (amounts are in the ledger rows above; empty-amount rows use the adjustment fills);
- set confident=false where the notes genuinely leave membership ambiguous.

Produce every clause and every component named in the specs."""

REPAIR_SUFFIX = """

RECONCILIATION FAILURE on a previous attempt: for component "{comp}" of clause {clause}
the auditor STATES {metric} = {target}, but the previous membership summed to {got}.
Re-examine that component's membership so it sums exactly to the stated figure
(other components: keep your best judgment)."""


def _fmt_rows(rows, cats, fills):
    out = []
    for r in rows:
        amt = r["amount"] if r["amount"] is not None else fills.get(r["txn_id"])
        out.append(f"{r['txn_id']} | {r['date']} | {r['counterparty'][:38]} | "
                   f"{r['description'][:48]} | {amt} {r.get('currency','USD')} | "
                   f"cat={cats.get(r['txn_id'])}")
    return "\n".join(out)


def _component_sum(txn_ids, off_ids, rows_by_id, fills, fx, off_by_id):
    total = 0.0
    for t in txn_ids:
        r = rows_by_id.get(t)
        if r is None:
            continue
        amt = r["amount"] if r["amount"] is not None else fills.get(t)
        if amt is None:
            continue
        if r.get("currency", "USD") != "USD":
            rate = fx.get(r["currency"])
            if rate is None:
                continue
            amt *= rate
        total += abs(amt)
    for oid in off_ids:
        o = off_by_id.get(oid)
        if o:
            total += o["amount"]
    return round(total, 2)


def _match_stated(comp_name, stated):
    """Best-effort mapping component name -> a stated figure, by keyword."""
    aliases = {
        "revenue": ["выручк", "revenue", "поступлени"],
        "opex": ["операционн", "operating expense"],
        "ebitda": ["ebitda"],
        "payroll": ["оплат", "персонал", "payroll", "фонд"],
        "capex": ["капитальн", "capex"],
    }
    keys = [k for k, words in aliases.items() if any(w in comp_name.lower() for w in [k])]
    words = aliases.get(keys[0] if keys else comp_name.lower(), [comp_name.lower()])
    for s in stated:
        if any(w in s["metric_name"].lower() for w in words):
            return s
    return None


def build_for_scenario(sid, facts, categories, ledger, scenario_facts):
    cov = facts["covenants"].get(sid)
    if not cov:
        return None, []
    audits = facts["audit"].get(sid, [])
    notes = "\n\n".join(
        (PARSED / f"{a['doc']}.txt").read_text()[:40000] for a in audits) or "[no notes]"
    stated = [s for a in audits for s in a.get("stated_figures", [])]

    cats = scenario_facts["categories"]
    fills = scenario_facts["amount_fills"]
    fx = scenario_facts["fx_rates"]
    off_by_id = {o["id"]: o for o in scenario_facts["off_ledger"]}
    rows_by_id = {r["txn_id"]: r for r in ledger[sid]}

    clauses_desc = json.dumps(
        [{"clause": c["clause"], "quote": c["quote"],
          "formula": c["formula"],
          "components": [{"name": x["name"],
                          "category_guess": x["definition"]["categories"]}
                         for x in c["components"]]}
         for c in cov["clauses"]], ensure_ascii=False)
    adjustments = json.dumps(
        {k: scenario_facts[k] for k in
         ("reclassifications", "exclusions", "amount_fills", "off_ledger",
          "ebitda_addbacks")}, ensure_ascii=False)
    base = PROMPT.format(clauses=clauses_desc, notes=notes,
                         adjustments=adjustments,
                         rows=_fmt_rows(ledger[sid], cats, fills))

    result = generate(base, model=STRONG, schema=SCHEMA, reasoning_effort="high")
    flags = []

    # deterministic reconciliation against stated figures, one repair round
    for cl in result.get("clauses", []):
        for comp in cl.get("components", []):
            s = _match_stated(comp["name"], stated)
            if s is None:
                continue
            got = _component_sum(comp["txn_ids"], comp["off_ledger_ids"],
                                 rows_by_id, fills, fx, off_by_id)
            if abs(got - s["amount_usd"]) <= 0.01:
                continue
            repaired = generate(
                base + REPAIR_SUFFIX.format(
                    comp=comp["name"], clause=cl["clause"],
                    metric=s["metric_name"], target=s["amount_usd"], got=got),
                model=STRONG, schema=SCHEMA, reasoning_effort="high")
            result = repaired
            comp2 = next(
                (c for rc in repaired.get("clauses", [])
                 if rc["clause"] == cl["clause"]
                 for c in rc.get("components", []) if c["name"] == comp["name"]),
                None)
            got2 = _component_sum(comp2["txn_ids"], comp2["off_ledger_ids"],
                                  rows_by_id, fills, fx, off_by_id) if comp2 else got
            if abs(got2 - s["amount_usd"]) > 0.01:
                flags.append({"scenario": sid, "type": "stated_figure_mismatch",
                              "detail": f"{cl['clause']}/{comp['name']}: stated "
                                        f"{s['amount_usd']} vs composed {got2}"})
            break  # one repair round per scenario is enough

    comp_map = {}
    for cl in result.get("clauses", []):
        entry = {}
        for c in cl.get("components", []):
            composed = _component_sum(c["txn_ids"], c["off_ledger_ids"],
                                      rows_by_id, fills, fx, off_by_id)
            s = _match_stated(c["name"], stated)
            entry[c["name"]] = {
                "txn_ids": c["txn_ids"],
                "off_ledger_ids": c["off_ledger_ids"],
                "basis_quote": c["basis_quote"],
                "confident": c["confident"],
                "composed_sum": composed,
                "stated_confirmed": bool(
                    s is not None and abs(composed - s["amount_usd"]) <= 0.01),
                "stated_figure": s["amount_usd"] if s else None,
            }
        comp_map[cl["clause"]] = entry
    return comp_map, flags


def main():
    import run_all
    ledger = json.loads((ART / "ledger_real.json").read_text())
    facts = json.loads((ART / "facts.json").read_text())
    categories = json.loads((ART / "categorized_ledger.json").read_text())

    def _one(sid):
        sf = run_all.build_scenario_facts(sid, ledger[sid], facts, categories, [])
        comp_map, flags = build_for_scenario(sid, facts, categories, ledger, sf)
        if comp_map:
            n = sum(len(v) for v in comp_map.values())
            print(f"{sid}: composition for {len(comp_map)} clauses / {n} components"
                  + (f", {len(flags)} mismatch flags" if flags else ""))
        return sid, comp_map, flags

    out, all_flags = {}, []
    for sid, comp_map, flags in pmap(_one, sorted(ledger)):
        all_flags += flags
        if comp_map:
            out[sid] = comp_map

    (ART / "composition.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    (ART / "composition_flags.json").write_text(json.dumps(all_flags, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
