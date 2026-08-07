#!/usr/bin/env python3
"""H1: cross-provider ensemble. Two independent blind runs (OpenAI + DeepSeek)
are combined WITHOUT ground truth:

- cells where both providers agree on status AND actual (<2% apart) -> take as-is;
- cells that disagree (status, or actual >2%) -> a strong-model ARBITER reads the
  verbatim clause + both independent computations + the real ledger rows and
  decides the verdict.

Inputs (saved before this runs):
    artifacts/ens_openai_answers.json / ens_openai_trails.json
    artifacts/ens_deepseek_answers.json / ens_deepseek_trails.json
Outputs:
    artifacts/answers_ensemble.json, artifacts/ensemble_report.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import STRONG, generate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ART = REPO / "artifacts"
TEMPLATE = REPO / "case-related-docs" / "submission_template.json"
REL_TOL = 0.02  # actuals within 2% count as agreement

ARB_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["COMPLIANT", "BREACH"]},
        "actual": {"type": "number"},
        "evidence_txn_id": {"type": ["string", "null"]},
        "chosen": {"type": "string", "enum": ["A", "B", "neither"]},
        "reasoning": {"type": "string"},
    },
    "required": ["status", "actual", "evidence_txn_id", "chosen", "reasoning"],
}

ARB_PROMPT = """Two independent systems computed the SAME loan covenant and disagree.
Read the verbatim clause and both computations, then decide the correct verdict.

VERBATIM CLAUSE:
{quote}

COMPUTATION A (system A):
  status={a_status}, actual={a_actual}, evidence={a_evidence}
  formula={a_formula}
  components={a_components}
  threshold={a_threshold}

COMPUTATION B (system B):
  status={b_status}, actual={b_actual}, evidence={b_evidence}
  formula={b_formula}
  components={b_components}
  threshold={b_threshold}

REAL LEDGER ROWS of this borrower (txn | date | counterparty | description | amount):
{rows}

Rules you must enforce:
- status is decided on the UNROUNDED metric; strict "не превышал/не менее" means
  exactly AT the limit COMPLIES; "менее/более" means at-limit already breaches;
- affiliation for related-party tests comes from the KYC dossier, not descriptions;
- actual is the metric's value: positive, 2 decimals (ratios as plain numbers);
- evidence = the single txn whose reversal flips the verdict, else null.

Return the correct status/actual/evidence, which computation you sided with
(A / B / neither if you computed fresh), and one line of reasoning."""


def rel_diff(x, y):
    d = max(abs(x), abs(y), 1e-9)
    return abs(x - y) / d


def load(name):
    p = ART / name
    return json.loads(p.read_text()) if p.exists() else {}


def arbiter(sid, clause, a, b, ta, tb, ledger):
    ca = ta.get(sid, {}).get("cells", {}).get(clause, {})
    cb = tb.get(sid, {}).get("cells", {}).get(clause, {})
    quote = cb.get("quote") or ca.get("quote") or f"clause {clause}"
    rows = "\n".join(
        f"{r['txn_id']} | {r['date']} | {r['counterparty'][:40]} | "
        f"{r['description'][:48]} | {r['amount']} {r.get('currency','')}"
        for r in ledger[sid])
    prompt = ARB_PROMPT.format(
        quote=quote,
        a_status=a["status"], a_actual=a["actual"], a_evidence=a["evidence_txn_id"],
        a_formula=ca.get("formula", "?"),
        a_components=json.dumps(ca.get("components", {}), ensure_ascii=False),
        a_threshold=json.dumps(ca.get("threshold", {}), ensure_ascii=False),
        b_status=b["status"], b_actual=b["actual"], b_evidence=b["evidence_txn_id"],
        b_formula=cb.get("formula", "?"),
        b_components=json.dumps(cb.get("components", {}), ensure_ascii=False),
        b_threshold=json.dumps(cb.get("threshold", {}), ensure_ascii=False),
        rows=rows)
    r = generate(prompt, model=STRONG, schema=ARB_SCHEMA, reasoning_effort="high")
    return {"status": r["status"], "actual": round(abs(r["actual"]), 2),
            "evidence_txn_id": r["evidence_txn_id"]}, r["chosen"], r["reasoning"]


def main():
    a_ans = load("ens_openai_answers.json")
    b_ans = load("ens_deepseek_answers.json")
    a_tr = load("ens_openai_trails.json")
    b_tr = load("ens_deepseek_trails.json")
    ledger = load("ledger_real.json")
    template = json.loads(TEMPLATE.read_text())

    ensemble, report = {}, []
    n_agree = n_arb = 0
    for sid, clauses in template["answers"].items():
        ensemble[sid] = {}
        for clause in clauses:
            a = a_ans.get(sid, {}).get(clause)
            b = b_ans.get(sid, {}).get(clause)
            if a is None or b is None:
                pick = a or b or {"status": "COMPLIANT", "actual": 1.0,
                                  "evidence_txn_id": None}
                ensemble[sid][clause] = pick
                report.append(f"{sid}/{clause}: only one provider -> take it")
                continue
            status_agree = a["status"] == b["status"]
            actual_close = rel_diff(a["actual"], b["actual"]) < REL_TOL
            if status_agree and actual_close:
                ensemble[sid][clause] = b  # both agree; DeepSeek was the better run
                n_agree += 1
                continue
            pick, chosen, why = arbiter(sid, clause, a, b, a_tr, b_tr, ledger)
            ensemble[sid][clause] = pick
            n_arb += 1
            report.append(
                f"{sid}/{clause}: A={a['status']}/{a['actual']} B={b['status']}/{b['actual']}"
                f" -> arbiter chose {chosen} => {pick['status']}/{pick['actual']} | {why[:90]}")

    (ART / "answers_ensemble.json").write_text(
        json.dumps(ensemble, indent=1, ensure_ascii=False))
    (ART / "ensemble_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False))
    print(f"ensemble: {n_agree} cells agreed, {n_arb} arbitrated")
    for line in report:
        print(" ", line[:160])


if __name__ == "__main__":
    main()
