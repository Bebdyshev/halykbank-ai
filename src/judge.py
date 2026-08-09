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
from llm import STRONG, generate, pmap  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ART = REPO / "artifacts"
ROUNDS = 2
NEAR_LIMIT_PCT = 5.0

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "cells": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["agree", "disagree", "uncertain"]},
                    "issues": {"type": "array", "items": {
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
                    }},
                },
                "required": ["clause", "verdict", "issues"],
            },
        },
        "confidence": {"type": "number"},
    },
    "required": ["cells", "confidence"],
}

JUDGE_PROMPT = """You are auditing ALL covenant verdicts of ONE borrower, produced by a
deterministic engine from LLM-extracted facts. For EACH clause below decide whether the
computation faithfully implements its verbatim text.

SHARED CONTEXT - ALL NON-NOISE LEDGER ROWS (with category provenance):
{rows}

ADJUSTMENTS APPLIED (audit reclassifications, exclusions, amount fills, off-ledger,
FX, EBITDA add-backs - each with its documentary quote):
{adjustments}

LARGEST ROWS EXCLUDED AS NOISE/DECOYS (re-including a wrongly-excluded one could
change a verdict - flag false_decoy if any looks like a genuine business row):
{noise_rows}

CELLS UNDER REVIEW (verbatim clause + machine spec + component values + engine result):
{cells}

Rules of the game you must enforce:
- affiliation comes from the KYC dossier, never from payment descriptions;
- only FINAL audit adjustments apply (rejected proposals never do);
- status is decided on the UNROUNDED metric;
- evidence must be the single txn whose fact-reversal flips the verdict (null for
  aggregate/ratio outcomes not driven by one fact-linked txn).

Return one entry per clause: agree/disagree/uncertain plus concrete issues
(empty issues list when you agree)."""

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


def _sane_evidence(ev, rows):
    """Collapse whatever the solver returned to ONE valid ledger txn id (or None)."""
    if not ev:
        return None
    valid = {r["txn_id"] for r in rows}
    if ev in valid:
        return ev
    for tok in __import__("re").findall(r"TXN[A-Za-z0-9-]*\d", ev):
        if tok in valid:
            return tok
    return None


def fallback_solve_voted(sid, clause, quote, facts_sf, rows, samples=5):
    """The fallback solver is the least deterministic piece of the pipeline
    (rehearsal showed its cells flip between runs). Majority vote on status,
    median actual within the majority, modal evidence."""
    results = []
    for i in range(samples):
        r = dict(fallback_solve(sid, clause, quote, facts_sf, rows, sample=i))
        results.append(r)
    from collections import Counter
    status = Counter(r["status"] for r in results).most_common(1)[0][0]
    winners = [r for r in results if r["status"] == status]
    actuals = sorted(abs(r["actual"]) for r in winners)
    actual = actuals[len(actuals) // 2]
    ev = Counter(_sane_evidence(r["evidence_txn_id"], rows)
                 for r in winners).most_common(1)[0][0]
    return {"status": status, "actual": actual, "evidence_txn_id": ev,
            "reasoning": f"vote {len(winners)}/{samples}: " + winners[0]["reasoning"]}


def _category_sums(rows, facts_sf):
    """Deterministic per-category |amount| totals (fills applied, NOISE and
    unlabeled rows excluded) - handed to the fallback solver so the LLM picks
    the formula composition but never does freehand arithmetic."""
    sums = {}
    fills = facts_sf.get("amount_fills", {})
    for r in rows:
        cat = facts_sf.get("categories", {}).get(r["txn_id"])
        amt = r["amount"] if r.get("amount") is not None else fills.get(r["txn_id"])
        if cat and cat != "NOISE" and amt is not None:
            sums[cat] = round(sums.get(cat, 0.0) + abs(amt), 2)
    return sums


def fallback_solve(sid, clause, quote, facts_sf, rows, sample=0):
    eff = []
    for r in rows:
        cat = facts_sf["categories"].get(r["txn_id"])
        eff.append(f"{r['txn_id']} | {r['date']} | {r['counterparty']} | "
                   f"{r['description']} | {r['amount']} | {r['currency']} | cat={cat}")
    prompt = (
        f"(independent attempt #{sample + 1}) "
        "Independently compute this covenant cell end-to-end. Show your arithmetic in "
        "'reasoning' and output the final status/actual/evidence. actual = the metric's "
        "value (positive, 2 decimals; ratios as plain numbers). Status decided on the "
        f"unrounded value.\n\nCLAUSE:\n{quote}\n\n"
        f"PRECOMPUTED CATEGORY SUMS (deterministic, fills applied, decoys excluded - "
        f"build the metric from THESE, do not re-add rows yourself):\n"
        f"{json.dumps(_category_sums(rows, facts_sf), ensure_ascii=False)}\n\n"
        f"ADJUSTMENT FACTS:\n{json.dumps({k: v for k, v in facts_sf.items() if k != 'categories'}, ensure_ascii=False)}\n\n"
        "LEDGER ROWS (with categories):\n" + "\n".join(eff))
    return generate(prompt, model=STRONG, schema=SOLVER_SCHEMA, reasoning_effort="high")


def _scenario_context(sid, trails, facts, ledger, answers):
    """One prompt covering every computed cell of a scenario (shared context sent once)."""
    sf = trails[sid]["facts"]

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

    comp_path = ART / "composition.json"
    composition = json.loads(comp_path.read_text()) if comp_path.exists() else {}

    all_flags = json.loads((ART / "flags.json").read_text()) \
        if (ART / "flags.json").exists() else []

    cells_fmt = []
    for spec in sorted(facts["covenants"][sid]["clauses"],
                       key=lambda c: c["clause"]):
        clause = spec["clause"]
        cell = trails[sid]["cells"].get(clause, {})
        ans = answers[sid].get(clause)
        if ans is None:
            errs = "; ".join(
                f["detail"] for f in all_flags
                if f.get("scenario") == sid and clause in str(f.get("detail", "")))
            engine_line = ("ENGINE RESULT: FAILED - no answer produced. "
                           f"Errors: {errs[:400] or 'unknown'}. The machine spec is "
                           "likely broken (empty component, bad period, sign error) - "
                           "report concrete spec issues so it can be repaired.")
        else:
            margin = cell.get("margin_pct")
            engine_line = (
                f"ENGINE RESULT: status={ans['status']}, actual={ans['actual']}, "
                f"evidence={ans['evidence_txn_id']}, "
                f"margin={None if margin is None else round(margin, 2)}%")
        cells_fmt.append(
            f"--- CLAUSE {clause} ---\n"
            f"VERBATIM: {cell.get('quote') or spec.get('quote', '')}\n"
            f"SPEC: {json.dumps({k: spec.get(k) for k in ('components', 'formula', 'threshold', 'condition')}, ensure_ascii=False)}\n"
            f"COMPONENT VALUES: {json.dumps(cell.get('components', {}), ensure_ascii=False)}\n"
            + engine_line
            + (f"\nAUDITOR-COMPOSITION (explicit membership used): "
               f"{json.dumps(composition[sid][clause], ensure_ascii=False)[:2000]}"
               if composition.get(sid, {}).get(clause) else ""))

    return JUDGE_PROMPT.format(
        rows="\n".join(contributors),
        adjustments=json.dumps({"applied": adj, "source_quotes": audit_quotes},
                               ensure_ascii=False)[:12000],
        noise_rows=noise_fmt,
        cells="\n\n".join(cells_fmt),
    )


def _kyc_gate_ok(sid, txn, new_lab, facts, ledger):
    """A judge relabel into RELATED_PARTY sticks only when the KYC dossier
    confirms it: counterparty matches an ownership entry at/above the
    dossier's threshold. Same discipline as H4 in run_all."""
    if new_lab.get("category") != "RELATED_PARTY":
        return True
    kyc = facts["kyc"].get(sid, {})
    threshold = kyc.get("related_party_threshold_pct")
    if threshold is None:
        return False
    inclusive = kyc.get("threshold_inclusive", True)
    import re as _re

    def norm(s):
        s = (s or "").lower()
        s = _re.sub(r"[\u00ab\u00bb\"'().,]", " ", s)
        s = _re.sub(r"\b(llp|jsc|llc|ltd|inc|\u0442\u043e\u043e|\u0430\u043e)\b", " ", s)
        return _re.sub(r"\s+", " ", s).strip()

    row = next((r for r in ledger[sid] if r["txn_id"] == txn), None)
    cp = norm(row["counterparty"]) if row else ""
    cand = norm(new_lab.get("kyc_matched_name") or "")
    for o in kyc.get("ownership", []):
        n = norm(o["name"])
        meets = o["pct"] >= threshold if inclusive else o["pct"] > threshold
        if n and (n == cand or n in cp or cp in n) and meets:
            return True
    return False


def _recompute_scenario(sid, facts, categories, ledger, meta, flags):
    sf = run_all.build_scenario_facts(sid, ledger[sid], facts, categories, flags)
    period = (meta.get(sid, {}).get("covenant_period") or {})
    composition = json.loads((ART / "composition.json").read_text()) \
        if (ART / "composition.json").exists() else {}
    out, trail = {}, {"facts": sf, "cells": {}}
    for clause_spec in facts["covenants"][sid]["clauses"]:
        # same deterministic backstops as run_all (NOISE strip, duplicate-summand
        # dedup, composition adoption gate, transfer-ratio, clause period) - a
        # judge recompute must never bypass them (that leaked the P8 double-count)
        covenant = run_all.build_covenant(sid, clause_spec, period, composition,
                                          ledger, flags)
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


def _spec_flip_guard(sid, candidate, facts, categories, ledger, answers):
    """Statuses that would flip if `candidate` replaced the consensus covenant
    spec. Repairing an absent cell is fine; flipping a computed one is not."""
    meta = json.loads((ART / "scenario_meta.json").read_text())
    trial = {**facts, "covenants": {**facts["covenants"], sid: candidate}}
    new_ans, _ = _recompute_scenario(sid, trial, categories, ledger, meta, [])
    flipped = []
    for cl, old in (answers.get(sid) or {}).items():
        new = new_ans.get(cl)
        if old is not None and new is not None and new["status"] != old["status"]:
            flipped.append(cl)
    return flipped


def _apply_issues(sid, clause, issues, facts, categories, ledger, report, answers):
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
        candidate = {"doc": doc, **new_spec}
        flipped = _spec_flip_guard(sid, candidate, facts, categories, ledger, answers)
        if flipped:
            report.append(f"{sid}/{clause}: re-extraction DENIED - would flip "
                          f"{flipped} on a single pass (consensus spec kept; "
                          "status changes need the voted solver)")
        else:
            facts["covenants"][sid] = candidate
            report.append(f"{sid}/{clause}: re-extracted covenants ({critique[:100]})")
            changed = True

    decoy_rehabs = [i for i in row_issues if i["type"] == "false_decoy"]
    other_rows = [i for i in row_issues if i["type"] == "suspect_category"]

    if other_rows:
        wanted = {t for i in other_rows for t in i["txn_ids"]}
        rows = [r for r in ledger[sid] if r["txn_id"] in wanted]
        if rows:
            kyc = facts["kyc"].get(sid, {})
            concerns = "; ".join(i["explanation"] for i in other_rows)
            relabel = generate(
                "Re-examine these ledger rows' categories in light of reviewer concerns.\n"
                f"CONCERNS: {concerns}\n"
                f"ROWS: {json.dumps(rows, ensure_ascii=False)}\n"
                f"KYC ownership: {json.dumps(kyc.get('ownership', []), ensure_ascii=False)}; "
                f"related-party rule: {kyc.get('threshold_quote')}\n"
                f"Categories: {json.dumps(extract.TAXONOMY)}",
                model=STRONG, schema=categorize.ROW_SCHEMA)
            for lab in relabel["labels"]:
                if lab["txn_id"] in wanted:
                    cur = (categories[sid].get(lab["txn_id"]) or {}).get("category")
                    if lab["category"] == "NOISE" and cur != "NOISE":
                        row = next(r for r in ledger[sid]
                                   if r["txn_id"] == lab["txn_id"])
                        confirm = generate(
                            "A reviewer wants to RECLASSIFY this ledger row as a "
                            "planted decoy (NOISE), deleting it from every covenant "
                            "metric. Presume the row is REAL unless its counterparty/"
                            "description pair is clearly incoherent for this borrower. "
                            "When in doubt, keep it real.\n"
                            f"REVIEWER'S ARGUMENT: {concerns}\n"
                            f"ROW: {json.dumps(row, ensure_ascii=False)}",
                            model=STRONG, schema=categorize.ROW_SCHEMA)
                        ok = any(l["txn_id"] == lab["txn_id"] and l["is_decoy"]
                                 for l in confirm.get("labels", []))
                        if not ok:
                            report.append(f"{sid}/{clause}: NOISE demotion DENIED "
                                          f"{lab['txn_id']} (presumed real)")
                            continue
                    if not _kyc_gate_ok(sid, lab["txn_id"], lab, facts, ledger):
                        report.append(f"{sid}/{clause}: relabel DENIED by KYC gate "
                                      f"{lab['txn_id']} -/-> RELATED_PARTY")
                        continue
                    categories[sid][lab["txn_id"]] = {**lab, "agree": False, "rejudged": True}
                    report.append(f"{sid}/{clause}: relabeled {lab['txn_id']} -> {lab['category']}")
                    changed = True

    # H2: rehabilitating a decoy (NOISE -> real) is dangerous - a false NOISE
    # deletes money, but a false rehab quietly pollutes every covenant. Gate it
    # behind a devil's-advocate check that must overcome an explicit presumption.
    if decoy_rehabs:
        wanted = {t for i in decoy_rehabs for t in i["txn_ids"]}
        rows = [r for r in ledger[sid] if r["txn_id"] in wanted]
        meta_all = json.loads((ART / "scenario_meta.json").read_text())
        industry = meta_all.get(sid, {}).get("industry", "unknown")
        for r in rows:
            check = generate(
                "A reviewer wants to RE-INCLUDE this ledger row previously marked as a "
                "planted decoy. Presume it IS a decoy unless the evidence clearly says "
                "otherwise. Rehabilitate ONLY if BOTH hold:\n"
                "1) the counterparty plausibly serves this borrower's specific industry "
                f"({industry});\n"
                "2) the description is coherent with what a company of that NAME would "
                "do (a stationery firm does not pay out insurance claims).\n"
                "Recycled generic-sounding counterparty names shared across many "
                "borrowers are decoys. When in doubt, keep NOISE.\n"
                f"REVIEWER'S ARGUMENT: {'; '.join(i['explanation'] for i in decoy_rehabs)}\n"
                f"ROW: {json.dumps(r, ensure_ascii=False)}",
                model=STRONG, schema=categorize.ROW_SCHEMA)
            for lab in check["labels"]:
                if lab["txn_id"] == r["txn_id"] and not lab["is_decoy"]                         and lab["category"] != "NOISE":
                    if not _kyc_gate_ok(sid, lab["txn_id"], lab, facts, ledger):
                        report.append(f"{sid}/{clause}: decoy rehab DENIED by KYC gate "
                                      f"{r['txn_id']}")
                        continue
                    categories[sid][r["txn_id"]] = {**lab, "agree": False, "rejudged": True}
                    report.append(f"{sid}/{clause}: decoy rehab CONFIRMED {r['txn_id']} -> {lab['category']}")
                    changed = True
                else:
                    report.append(f"{sid}/{clause}: decoy rehab DENIED {r['txn_id']} (stays NOISE)")

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


def _apply_sovereignty(answers, answers_before, template, report):
    """Engine sovereignty: a cell the deterministic engine computed is FINAL.
    Measured across 9 disclosed GT runs: judge/solver status flips of computed
    cells were wrong 6/6, and even actual 'refinements' hurt (P2 1.18->1.16).
    The only accepted post-judge improvement is ADDING evidence where the
    engine had none. Absent cells keep judge repairs / solver fills."""
    for sid, clauses in template["answers"].items():
        for clause in clauses:
            before = answers_before.get(sid, {}).get(clause)
            after = answers.get(sid, {}).get(clause)
            if before is None or after == before:
                continue
            restored = dict(before)
            if restored.get("evidence_txn_id") is None and after \
                    and after.get("evidence_txn_id"):
                restored["evidence_txn_id"] = after["evidence_txn_id"]
            answers.setdefault(sid, {})[clause] = restored
            report.append(
                f"{sid}/{clause}: judge_flip_reverted -> engine answer restored "
                f"(judged was {after and after.get('status')}/"
                f"{after and after.get('actual')})")


def main() -> None:
    ledger = json.loads((ART / "ledger_real.json").read_text())
    facts = json.loads((ART / "facts.json").read_text())
    categories = json.loads((ART / "categorized_ledger.json").read_text())
    meta = json.loads((ART / "scenario_meta.json").read_text())
    answers = json.loads((ART / "answers.json").read_text())
    trails = json.loads((ART / "audit_trails.json").read_text())
    flags = json.loads((ART / "flags.json").read_text())

    report = []
    import copy
    answers_before = copy.deepcopy(answers)
    template = json.loads(
        (REPO / "case-related-docs" / "submission_template.json").read_text())

    # ---- Stage A: deterministic invariants -> cells needing attention
    attention = set()
    for f in flags:
        sid = f.get("scenario")
        if f.get("type") == "composition_divergence":
            continue  # recorded for the log; must not flood judge attention
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

    # ---- Stage B: judge per SCENARIO (one batched call for all its cells).
    # Verdict calls run in PARALLEL (read-only); fixes apply sequentially
    # afterwards because they mutate shared facts/categories.
    seen_issues = set()
    for rnd in range(1, ROUNDS + 1):
        n_disagree = 0
        todo = [sid for sid in sorted(template["answers"])
                if sid in answers and sid in trails
                and (rnd == 1 or any((sid, c) in attention
                                     for c in template["answers"][sid]))]
        verdicts = dict(zip(todo, pmap(
            lambda s: generate(_scenario_context(s, trails, facts, ledger, answers),
                               model=STRONG, schema=JUDGE_SCHEMA,
                               reasoning_effort="high"),
            todo)))
        for sid in todo:
            result = verdicts[sid]
            cell_verdicts = {c["clause"]: c for c in result.get("cells", [])}
            scenario_changed = False
            pending_solves = []
            for clause in sorted(template["answers"][sid]):
                v = cell_verdicts.get(clause)
                if v is None:
                    continue
                if v["verdict"] == "agree" \
                        and answers.get(sid, {}).get(clause) is not None:
                    attention.discard((sid, clause))
                    continue
                n_disagree += 1
                attention.add((sid, clause))
                report.append(
                    f"round{rnd} {sid}/{clause}: {v['verdict']} - "
                    + "; ".join(f"{i['type']}:{i['explanation'][:80]}"
                                for i in v["issues"]))
                fresh = [i for i in v["issues"]
                         if (sid, clause, i["type"], i["explanation"][:60])
                         not in seen_issues]
                for i in fresh:
                    seen_issues.add((sid, clause, i["type"], i["explanation"][:60]))

                # Conservative B6 routing (measured: routing computed cells to the
                # voted solver flipped near-limit plants P2/P5/P7 - the solver is
                # noisier than the deterministic engine there). Spec doubts go to
                # _apply_issues re-extraction under the flip-guard; only genuinely
                # unmodelable rules reach the solver.
                if [i for i in fresh if i["type"] == "unmodeled_rule_material"]:
                    pending_solves.append(clause)
                if fresh and _apply_issues(sid, clause, fresh,
                                           facts, categories, ledger, report,
                                           answers):
                    scenario_changed = True
            if scenario_changed:
                new_ans, new_trail = _recompute_scenario(
                    sid, facts, categories, ledger, meta, flags)
                answers[sid] = new_ans
                trails[sid] = new_trail
            # solver overrides AFTER the recompute so they cannot be clobbered
            for clause in pending_solves:
                spec = next((c for c in facts["covenants"][sid]["clauses"]
                             if c["clause"] == clause), None)
                solved = fallback_solve_voted(
                    sid, clause, (spec or {}).get("quote", ""),
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

    # ---- engine sovereignty (see _apply_sovereignty docstring)
    _apply_sovereignty(answers, answers_before, template, report)

    # ---- fill any still-missing cells with the fallback solver (empty = wrong)
    for sid, clauses in template["answers"].items():
        for clause in clauses:
            if answers.get(sid, {}).get(clause) is None:
                spec = next((c for c in facts.get("covenants", {}).get(sid, {})
                             .get("clauses", []) if c["clause"] == clause), None)
                quote = (spec or {}).get("quote", f"clause {clause}")
                sf = trails.get(sid, {}).get("facts") or {"categories": {}}
                solved = fallback_solve_voted(sid, clause, quote, sf, ledger[sid])
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
    (ART / "audit_trails.json").write_text(
        json.dumps(trails, indent=1, ensure_ascii=False))
    (ART / "judge_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False))
    print(f"judge done; {len(report)} report lines")
    for line in report:
        print(" ", line[:160])


if __name__ == "__main__":
    main()
