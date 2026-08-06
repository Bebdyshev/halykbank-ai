#!/usr/bin/env python3
"""Self-correction WITHOUT ground truth: per-cell LLM judge + targeted re-runs.

Stage A (code): invariant flags - missing cells, near-limit margins, plus every
flag run_all.py raised (unresolved adjustments, unmodeled conditions, ...).
Stage B (gpt-5): every cell is reviewed against the verbatim clause text, the
computation trail and the excluded-as-noise rows. Disagreements route to
targeted re-runs (re-extraction / row re-categorization / audit re-read /
independent fallback solve), max ROUNDS per cell.

Outputs: artifacts/answers_judged.json, artifacts/judge_report.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import categorize  # noqa: E402
import extract  # noqa: E402
import run_all  # noqa: E402
from compute import compute_cell  # noqa: E402
from llm import STRONG, generate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ART = REPO / "artifacts"
ROUNDS = 2
NEAR_LIMIT_PCT = 2.5

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["agree", "disagree", "uncertain"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": [
                        "wrong_formula", "wrong_threshold", "wrong_strictness",
                        "missed_adjustment", "suspect_category", "false_decoy",
                        "period_error", "fx_error", "evidence_invalid",
                        "unmodeled_rule_material"]},
                    "txn_ids": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "string"},
                },
                "required": ["type", "txn_ids", "explanation"],
            },
        },
        "confidence": {"type": "number"},
    },
    "required": ["verdict", "issues", "confidence"],
}

JUDGE_PROMPT = """You are auditing ONE covenant verdict produced by a deterministic engine
from LLM-extracted facts. Decide whether the computation faithfully implements the clause.

VERBATIM CLAUSE:
{quote}

MACHINE SPEC USED (components / formula / threshold / condition):
{spec}

COMPONENT VALUES AND TOP CONTRIBUTING TRANSACTIONS (with category provenance):
{components}

ADJUSTMENTS APPLIED (audit reclassifications, exclusions, amount fills, off-ledger,
FX, EBITDA add-backs - each with its documentary quote):
{adjustments}

LARGEST ROWS EXCLUDED AS NOISE/DECOYS (re-including a wrongly-excluded one could
change the verdict - flag false_decoy if any looks like a genuine business row):
{noise_rows}

ENGINE RESULT: status={status}, actual={actual}, evidence={evidence},
margin vs limit = {margin}%.

Rules of the game you must enforce:
- affiliation comes from the KYC dossier, never from payment descriptions;
- only FINAL audit adjustments apply (rejected proposals never do);
- status is decided on the UNROUNDED metric;
- evidence must be the single txn whose fact-reversal flips the verdict (null for
  aggregate/ratio outcomes not driven by one fact-linked txn).

Report agree/disagree/uncertain plus concrete issues. Empty issues list if agree."""

SOLVER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["COMPLIANT", "BREACH"]},
        "actual": {"type": "number"},
        "evidence_txn_id": {"type": ["string", "null"]},
        "reasoning": {"type": "string"},
    },
    "required": ["status", "actual", "evidence_txn_id", "reasoning"],
}


def fallback_solve(sid, clause, quote, facts_sf, rows):
    eff = []
    for r in rows:
        cat = facts_sf["categories"].get(r["txn_id"])
        eff.append(f"{r['txn_id']} | {r['date']} | {r['counterparty']} | "
                   f"{r['description']} | {r['amount']} | {r['currency']} | cat={cat}")
    prompt = (
        "Independently compute this covenant cell end-to-end. Show your arithmetic in "
        "'reasoning' and output the final status/actual/evidence. actual = the metric's "
        "value (positive, 2 decimals; ratios as plain numbers). Status decided on the "
        f"unrounded value.\n\nCLAUSE:\n{quote}\n\n"
        f"ADJUSTMENT FACTS:\n{json.dumps({k: v for k, v in facts_sf.items() if k != 'categories'}, ensure_ascii=False)}\n\n"
        "LEDGER ROWS (with categories):\n" + "\n".join(eff))
    return generate(prompt, model=STRONG, schema=SOLVER_SCHEMA, reasoning_effort="high")


def _cell_context(sid, clause, trails, facts, ledger, answers):
    cell = trails[sid]["cells"][clause]
    sf = trails[sid]["facts"]
    rows_by_id = {r["txn_id"]: r for r in ledger[sid]}

    comp_lines = []
    for name, value in cell["components"].items():
        comp_lines.append(f"{name} = {value}")
    contributors = []
    for r in ledger[sid]:
        cat = sf["categories"].get(r["txn_id"])
        if cat and cat != "NOISE":
            contributors.append(
                f"  {r['txn_id']} | {r['counterparty'][:40]} | {r['description'][:50]} "
                f"| {r['amount']} | cat={cat}")
    noise_rows = sorted(
        (r for r in ledger[sid] if sf["categories"].get(r["txn_id"]) == "NOISE"),
        key=lambda r: -abs(r["amount"] or 0))[:10]
    noise_fmt = "\n".join(
        f"  {r['txn_id']} | {r['counterparty'][:40]} | {r['description'][:50]} | {r['amount']}"
        for r in noise_rows) or "  (none)"

    adj = {k: sf[k] for k in ("reclassifications", "exclusions", "amount_fills",
                              "off_ledger", "fx_rates", "ebitda_addbacks")}
    audit_quotes = [
        {"doc": a["doc"], "reclass": a.get("reclassifications"),
         "rejected": a.get("rejected_reclassifications"),
         "excl": a.get("exclusions"), "fills": a.get("amount_fills"),
         "off_ledger": a.get("off_ledger"), "addbacks": a.get("ebitda_addbacks")}
        for a in facts["audit"].get(sid, [])]

    ans = answers[sid][clause]
    spec = next(c for c in facts["covenants"][sid]["clauses"] if c["clause"] == clause)
    return JUDGE_PROMPT.format(
        quote=cell.get("quote") or spec.get("quote", ""),
        spec=json.dumps({k: spec[k] for k in
                         ("components", "formula", "threshold", "condition")},
                        ensure_ascii=False),
        components="\n".join(comp_lines) + "\nALL NON-NOISE ROWS:\n" + "\n".join(contributors),
        adjustments=json.dumps({"applied": adj, "source_quotes": audit_quotes},
                               ensure_ascii=False)[:12000],
        noise_rows=noise_fmt,
        status=ans["status"], actual=ans["actual"], evidence=ans["evidence_txn_id"],
        margin=None if cell.get("margin_pct") is None else round(cell["margin_pct"], 2),
    )


def _recompute_scenario(sid, facts, categories, ledger, meta, flags):
    sf = run_all.build_scenario_facts(sid, ledger[sid], facts, categories, flags)
    period = (meta.get(sid, {}).get("covenant_period") or {})
    out, trail = {}, {"facts": sf, "cells": {}}
    for clause_spec in facts["covenants"][sid]["clauses"]:
        covenant = {
            "components": {c["name"]: c["definition"] for c in clause_spec["components"]},
            "formula": clause_spec["formula"],
            "threshold": clause_spec["threshold"],
        }
        if period.get("start") and period.get("end"):
            covenant["period"] = [period["start"], period["end"]]
        if clause_spec.get("condition"):
            covenant["condition"] = clause_spec["condition"]
        try:
            r = compute_cell(covenant, ledger[sid], sf)
        except Exception as e:  # noqa: BLE001
            flags.append({"scenario": sid, "type": "engine_error",
                          "detail": f"{clause_spec['clause']}: {e}"})
            continue
        out[clause_spec["clause"]] = {k: r[k] for k in ("status", "actual", "evidence_txn_id")}
        trail["cells"][clause_spec["clause"]] = {**r["computation"],
                                                 "quote": clause_spec.get("quote", "")}
    return out, trail


def _apply_issues(sid, clause, issues, facts, categories, ledger, report):
    """Route judge issues to targeted re-runs. Returns True if anything changed."""
    changed = False
    spec_issues = [i for i in issues if i["type"] in
                   ("wrong_formula", "wrong_threshold", "wrong_strictness", "period_error")]
    row_issues = [i for i in issues if i["type"] in ("suspect_category", "false_decoy")]
    audit_issues = [i for i in issues if i["type"] in ("missed_adjustment", "fx_error")]

    if spec_issues:
        critique = "; ".join(i["explanation"] for i in spec_issues)
        doc = facts["covenants"][sid]["doc"]
        base = extract.COVENANT_PROMPT.format(
            clause_keys=extract.clause_keys_for(sid),
            taxonomy=json.dumps(extract.TAXONOMY),
            text=extract._doc_text(doc))
        new_spec = generate(
            base + f"\n\nA reviewer raised these concerns about a previous extraction - "
                   f"weigh them against the text:\n{critique}",
            model=STRONG, schema=extract.COVENANT_SCHEMA, reasoning_effort="high")
        facts["covenants"][sid] = {"doc": doc, **new_spec}
        report.append(f"{sid}/{clause}: re-extracted covenants ({critique[:100]})")
        changed = True

    for issue in row_issues:
        for txn in issue["txn_ids"]:
            row = next((r for r in ledger[sid] if r["txn_id"] == txn), None)
            if row is None:
                continue
            kyc = facts["kyc"].get(sid, {})
            relabel = generate(
                "Re-examine ONE ledger row's category in light of a reviewer concern.\n"
                f"CONCERN: {issue['explanation']}\n"
                f"ROW: {json.dumps(row, ensure_ascii=False)}\n"
                f"KYC ownership: {json.dumps(kyc.get('ownership', []), ensure_ascii=False)}; "
                f"related-party rule: {kyc.get('threshold_quote')}\n"
                f"Categories: {json.dumps(extract.TAXONOMY)}",
                model=STRONG, schema=categorize.ROW_SCHEMA)
            for lab in relabel["labels"]:
                if lab["txn_id"] == txn:
                    categories[sid][txn] = {**lab, "agree": False, "rejudged": True}
                    report.append(f"{sid}/{clause}: relabeled {txn} -> {lab['category']}")
                    changed = True

    if audit_issues:
        hints = "; ".join(i["explanation"] for i in audit_issues)
        for entry in facts["audit"].get(sid, []):
            doc = entry["doc"]
            renewed = generate(
                "Re-extract adjustments from this auditor/treasury document. A reviewer "
                f"suspects something was missed: {hints}\nReport ONLY what the document "
                "states, with verbatim quotes.\n\nDOCUMENT TEXT:\n" + extract._doc_text(doc),
                model=STRONG, schema=extract.AUDIT_SCHEMA)
            entry.update(renewed)
        if facts["audit"].get(sid):
            report.append(f"{sid}/{clause}: re-read audit docs ({hints[:100]})")
            changed = True
    return changed


def main() -> None:
    ledger = json.loads((ART / "ledger_real.json").read_text())
    facts = json.loads((ART / "facts.json").read_text())
    categories = json.loads((ART / "categorized_ledger.json").read_text())
    meta = json.loads((ART / "scenario_meta.json").read_text())
    answers = json.loads((ART / "answers.json").read_text())
    trails = json.loads((ART / "audit_trails.json").read_text())
    flags = json.loads((ART / "flags.json").read_text())

    report = []
    template = json.loads(
        (REPO / "case-related-docs" / "submission_template.json").read_text())

    # ---- Stage A: deterministic invariants -> cells needing attention
    attention = set()
    for f in flags:
        sid = f.get("scenario")
        if sid in template["answers"]:
            for clause in template["answers"][sid]:
                attention.add((sid, clause))
    for sid, clauses in template["answers"].items():
        for clause in clauses:
            cell = answers.get(sid, {}).get(clause)
            if cell is None:
                attention.add((sid, clause))
                continue
            m = trails.get(sid, {}).get("cells", {}).get(clause, {}).get("margin_pct")
            if m is not None and abs(m) < NEAR_LIMIT_PCT:
                attention.add((sid, clause))
                report.append(f"{sid}/{clause}: near-limit margin {round(m, 2)}% -> review")

    # ---- Stage B: judge EVERY computed cell; re-run targeted fixes
    for rnd in range(1, ROUNDS + 1):
        n_disagree = 0
        for sid in sorted(template["answers"]):
            for clause in sorted(template["answers"][sid]):
                if rnd > 1 and (sid, clause) not in attention:
                    continue
                if answers.get(sid, {}).get(clause) is None:
                    continue  # handled by fallback pass below
                prompt = _cell_context(sid, clause, trails, facts, ledger, answers)
                verdict = generate(prompt, model=STRONG, schema=JUDGE_SCHEMA,
                                   reasoning_effort="high")
                if verdict["verdict"] == "agree":
                    attention.discard((sid, clause))
                    continue
                n_disagree += 1
                attention.add((sid, clause))
                report.append(
                    f"round{rnd} {sid}/{clause}: {verdict['verdict']} - "
                    + "; ".join(f"{i['type']}:{i['explanation'][:80]}"
                                for i in verdict["issues"]))
                unmodeled = [i for i in verdict["issues"]
                             if i["type"] == "unmodeled_rule_material"]
                changed = _apply_issues(sid, clause, verdict["issues"],
                                        facts, categories, ledger, report)
                if changed:
                    new_ans, new_trail = _recompute_scenario(
                        sid, facts, categories, ledger, meta, flags)
                    answers[sid] = new_ans
                    trails[sid] = new_trail
                if unmodeled:
                    spec = next(c for c in facts["covenants"][sid]["clauses"]
                                if c["clause"] == clause)
                    solved = fallback_solve(sid, clause, spec.get("quote", ""),
                                            trails[sid]["facts"], ledger[sid])
                    eng = answers.get(sid, {}).get(clause)
                    if eng is None or solved["status"] != eng["status"]:
                        answers.setdefault(sid, {})[clause] = {
                            "status": solved["status"],
                            "actual": round(abs(solved["actual"]), 2),
                            "evidence_txn_id": solved["evidence_txn_id"]}
                        report.append(f"{sid}/{clause}: fallback solver override "
                                      f"({solved['reasoning'][:120]})")
        if n_disagree == 0:
            break

    # ---- fill any still-missing cells with the fallback solver (empty = wrong)
    for sid, clauses in template["answers"].items():
        for clause in clauses:
            if answers.get(sid, {}).get(clause) is None:
                spec = next((c for c in facts.get("covenants", {}).get(sid, {})
                             .get("clauses", []) if c["clause"] == clause), None)
                quote = (spec or {}).get("quote", f"clause {clause}")
                sf = trails.get(sid, {}).get("facts") or {"categories": {}}
                solved = fallback_solve(sid, clause, quote, sf, ledger[sid])
                answers.setdefault(sid, {})[clause] = {
                    "status": solved["status"],
                    "actual": round(abs(solved["actual"]), 2),
                    "evidence_txn_id": solved["evidence_txn_id"]}
                report.append(f"{sid}/{clause}: filled by fallback solver")

    (ART / "answers_judged.json").write_text(
        json.dumps(answers, indent=1, ensure_ascii=False))
    (ART / "facts.json").write_text(json.dumps(facts, indent=1, ensure_ascii=False))
    (ART / "categorized_ledger.json").write_text(
        json.dumps(categories, indent=1, ensure_ascii=False))
    (ART / "judge_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False))
    print(f"judge done; {len(report)} report lines")
    for line in report:
        print(" ", line[:160])


if __name__ == "__main__":
    main()
