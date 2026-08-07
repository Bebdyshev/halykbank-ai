#!/usr/bin/env python3
"""S4 CATEGORIZE: label every real ledger row. The LLM makes ALL the calls -
category, KYC match kind, decoy status - with deterministic signals passed as
hints, never as decisions.

Passes: gpt-5 (full context) + gpt-5-mini (shuffled order). Category
disagreements go to a gpt-5 tie-break; is_decoy disagreements ALWAYS escalate
(a false NOISE silently deletes money from every covenant it touched).

Output: artifacts/categorized_ledger.json
    {scenario: {txn_id: {category, match_kind, kyc_matched_name, is_decoy, agree}}}
"""
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import TAXONOMY  # noqa: E402
from llm import CHEAP, STRONG, generate, pmap  # noqa: F401,E402

REPO = Path(__file__).resolve().parent.parent

ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": "string"},
                    "category": {"type": "string", "enum": TAXONOMY},
                    "kyc_matched_name": {"type": ["string", "null"],
                                         "description": "exact KYC table name this counterparty is, or null"},
                    "match_kind": {"type": "string", "enum": [
                        "affiliate", "subsidiary_restricted",
                        "subsidiary_unrestricted", "none"]},
                    "is_decoy": {"type": "boolean"},
                    "decoy_reason": {"type": ["string", "null"]},
                },
                "required": ["txn_id", "category", "kyc_matched_name",
                             "match_kind", "is_decoy", "decoy_reason"],
            },
        },
    },
    "required": ["labels"],
}

PROMPT = """You are categorizing bank-ledger transactions of the borrower "{company}"
({industry}) for loan-covenant testing. Assign EVERY transaction exactly one category:

{taxonomy}

BORROWER'S KYC DOSSIER (authoritative for affiliation - never judge affiliation by the
payment description):
- ownership table (counterparty -> group's voting stake): {ownership}
- related-party rule: {threshold_quote}
- subsidiary collateral table (subsidiary -> % of assets pledged): {subsidiaries}
- subsidiary perimeter rule: {perimeter_quote}

Decision guide:
1. kyc_matched_name + match_kind: if the counterparty IS one of the KYC-table entities
   (allow name variants/suffixes), record which and classify the relationship using the
   dossier's own numeric rules: affiliate = ownership at/above the dossier's related-party
   threshold; subsidiary_restricted / subsidiary_unrestricted per the perimeter rule.
   Otherwise match_kind=none.
2. category RELATED_PARTY: ONLY for PAYMENTS (negative amounts) to an affiliate per rule 1.
   Money RECEIVED from an affiliate is whatever it economically is (usually REVENUE).
3. category SUBSIDIARY_TRANSFER: transfers of assets/equipment to the borrower's own
   subsidiaries (any perimeter status - record the status in match_kind).
4. is_decoy: this dataset plants fake rows. A real row has a coherent
   counterparty <-> description <-> industry triple. A decoy has an incoherent pair
   (a stationery firm paying interest income, an office-supplies company receiving an
   insurance premium) or a generic counterparty with no plausible relation to the
   borrower's business. The hint column shows in how many OTHER borrowers' ledgers the
   same counterparty-name stem appears - real counterparties are typically specific to
   one borrower, decoy names are recycled across many; treat it as evidence, not proof.
   Decoys get category NOISE.
5. OTHER_INFLOW: positive amounts that are refunds, deposit returns, VAT refunds,
   sweep-backs, credits - NOT revenue. REVENUE only for genuine operating revenue of the
   core business.
6. Otherwise the most specific expense category; OPEX for genuine operating costs with no
   specific bucket; FINANCING_INFLOW for loan drawdowns.

Transactions (txn_id | date | counterparty | description | amount | cross-borrower-stem-count):
{rows}

Label every txn_id exactly once."""


def _stem_counts(all_ledger: dict) -> dict:
    stem_scen = defaultdict(set)
    for sid, rows in all_ledger.items():
        for r in rows:
            stem = re.sub(r"\(.*?\)", "", r["counterparty"]).split()[0].lower()
            stem_scen[stem].add(sid)
    return {s: len(v) for s, v in stem_scen.items()}


def _fmt_rows(rows, stem_counts):
    out = []
    for r in rows:
        stem = re.sub(r"\(.*?\)", "", r["counterparty"]).split()[0].lower()
        out.append(f"{r['txn_id']} | {r['date']} | {r['counterparty']} | "
                   f"{r['description']} | {r['amount']} | seen_in_{stem_counts.get(stem, 1)}_borrowers")
    return "\n".join(out)


def label_pass(rows, ctx, stem_counts, model, shuffle_seed=None):
    rows = rows[:]
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(rows)
    prompt = PROMPT.format(
        company=ctx["company"], industry=ctx["industry"],
        taxonomy="\n".join(f"- {t}" for t in TAXONOMY),
        ownership=json.dumps(ctx["ownership"], ensure_ascii=False),
        threshold_quote=ctx["threshold_quote"] or "[not stated]",
        subsidiaries=json.dumps(ctx["subsidiaries"], ensure_ascii=False),
        perimeter_quote=ctx["perimeter_quote"] or "[not stated]",
        rows=_fmt_rows(rows, stem_counts),
    )
    result = generate(prompt, model=model, schema=ROW_SCHEMA)
    return {x["txn_id"]: x for x in result["labels"]}


def main() -> None:
    ledger = json.loads((REPO / "artifacts" / "ledger_real.json").read_text())
    facts = json.loads((REPO / "artifacts" / "facts.json").read_text())
    meta = json.loads((REPO / "artifacts" / "scenario_meta.json").read_text())
    stem_counts = _stem_counts(ledger)

    def _scenario(sid):
        rows = ledger[sid]
        kyc = facts["kyc"].get(sid, {})
        sm = meta.get(sid, {})
        ctx = {
            "company": sm.get("borrower_name", sid),
            "industry": sm.get("industry", "unknown"),
            "ownership": kyc.get("ownership", []),
            "threshold_quote": kyc.get("threshold_quote"),
            "subsidiaries": kyc.get("subsidiaries", []),
            "perimeter_quote": (kyc.get("perimeter_rule") or {}).get("quote"),
        }

        both = pmap(lambda args: label_pass(rows, ctx, stem_counts, *args),
                    [(STRONG,), (CHEAP, 42)], max_workers=2)
        p1, p2 = both[0], both[1]

        labels = {}
        disputed = []
        n_cat, n_decoy = 0, 0
        for r in rows:
            txn = r["txn_id"]
            a, b = p1.get(txn), p2.get(txn)
            if a is None or b is None:
                labels[txn] = {**(a or b), "agree": False}
                continue
            cat_agree = a["category"] == b["category"]
            decoy_agree = a["is_decoy"] == b["is_decoy"]
            if cat_agree and decoy_agree:
                labels[txn] = {**a, "agree": True}
                continue
            n_cat += (not cat_agree)
            n_decoy += (not decoy_agree)
            disputed.append((r, a, b))

        # ONE batched tie-break call for all disputed rows of the scenario
        if disputed:
            listing = "\n".join(
                f"{r['txn_id']} | {r['date']} | {r['counterparty']} | {r['description']} "
                f"| {r['amount']} | candidate A: {a['category']}(decoy={a['is_decoy']}) "
                f"| candidate B: {b['category']}(decoy={b['is_decoy']})"
                for r, a, b in disputed)
            tie_prompt = PROMPT.format(
                company=ctx["company"], industry=ctx["industry"],
                taxonomy="\n".join(f"- {t}" for t in TAXONOMY),
                ownership=json.dumps(ctx["ownership"], ensure_ascii=False),
                threshold_quote=ctx["threshold_quote"] or "[not stated]",
                subsidiaries=json.dumps(ctx["subsidiaries"], ensure_ascii=False),
                perimeter_quote=ctx["perimeter_quote"] or "[not stated]",
                rows=listing,
            ) + ("\n\nTwo previous passes disagreed on these rows (their candidate labels "
                 "are shown). Decide the final label for each row.")
            tie = generate(tie_prompt, model=STRONG, schema=ROW_SCHEMA)
            tie_by_id = {x["txn_id"]: x for x in tie["labels"]}
            for r, a, b in disputed:
                pick = tie_by_id.get(r["txn_id"], a)
                labels[r["txn_id"]] = {**pick, "agree": False,
                                       "pass1": a["category"], "pass2": b["category"]}
        print(f"{sid}: {len(rows)} rows, {n_cat} category / {n_decoy} decoy disagreements"
              f" -> 1 batched tie-break call")
        return sid, labels

    out = dict(pmap(_scenario, sorted(ledger)))

    (REPO / "artifacts" / "categorized_ledger.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
