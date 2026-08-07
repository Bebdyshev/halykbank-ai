#!/usr/bin/env python3
"""S3 EXTRACT: LLM converts documents into typed JSON facts.

Extractors (all grounded with verbatim quotes):
    extract_kyc         ownership table, related-party threshold, subsidiary perimeter RULE
    extract_audit       FINAL-audit adjustments (reclass/exclusions/fills/off-ledger/FX/add-backs)
    extract_covenants   clauses of the ACTIVE agreement -> compute.py DSL (2 passes + arbiter)
    extract_group       consolidated group reports: attribution + PP&E movement

Clause keys come from submission_template.json - nothing about "6.1..6.3" is
assumed. Category taxonomy is FIXED and shared with the categorization stage.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import CHEAP, STRONG, generate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PARSED = REPO / "artifacts" / "parsed_docs"
TEMPLATE = REPO / "case-related-docs" / "submission_template.json"
MAX_DOC_CHARS = 150000

TAXONOMY = [
    "REVENUE",            # operating revenue from customers (NOT refunds/deposits/VAT returns)
    "OPEX",               # operating expenses not in a more specific bucket below
    "PAYROLL",            # wages, salaries, personnel costs, severance payments
    "UTILITIES",          # electricity, water, heat, communications
    "INSURANCE",          # insurance premiums
    "RENT_LEASE",         # rent and lease payments
    "TAX",                # taxes and mandatory state payments
    "INTEREST",           # interest / financing costs / debt service
    "CAPEX",              # purchase of equipment, machinery, construction, vehicles
    "CONSULTING",         # advisory / consulting fees
    "MARKETING",          # advertising, media
    "FINANCING_INFLOW",   # loan drawdowns / credit facility inflows
    "RELATED_PARTY",      # payment to a counterparty affiliated per the KYC dossier
    "SUBSIDIARY_TRANSFER",# asset/equipment transfers to the borrower's subsidiaries
    "OTHER_INFLOW",       # refunds, deposit returns, VAT refunds, sweep-backs - NOT revenue
    "NOISE",              # planted decoy row - contributes to nothing
    "GROUP_CAPEX",        # GROUP-level consolidated capex (from holding reports, not the ledger)
]

# ---------------------------------------------------------------- KYC

KYC_SCHEMA = {
    "type": "object",
    "properties": {
        "ownership": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "pct": {"type": "number"}},
                "required": ["name", "pct"],
            },
        },
        "related_party_threshold_pct": {"type": ["number", "null"]},
        "threshold_quote": {"type": "string"},
        "subsidiaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "pledged_pct": {"type": "number"}},
                "required": ["name", "pledged_pct"],
            },
        },
        "perimeter_rule": {
            "type": ["object", "null"],
            "description": "the dossier's own rule for restricted vs unrestricted subsidiaries",
            "properties": {
                "cut_pct": {"type": "number"},
                "above_or_equal_treatment": {"type": "string", "enum": ["restricted", "unrestricted"]},
                "below_treatment": {"type": "string", "enum": ["restricted", "unrestricted"]},
                "quote": {"type": "string"},
            },
            "required": ["cut_pct", "above_or_equal_treatment", "below_treatment", "quote"],
        },
    },
    "required": ["ownership", "related_party_threshold_pct", "threshold_quote",
                 "subsidiaries", "perimeter_rule"],
}


def extract_kyc(doc_hash: str) -> dict:
    prompt = (
        "Extract related-party facts from this KYC dossier. Report:\n"
        "1) the ownership table - every counterparty with its voting-rights %;\n"
        "2) the dossier's own numeric threshold for what counts as a related party "
        "(null if the dossier states no such rule);\n"
        "3) the subsidiary collateral table (name + pledged %) if present;\n"
        "4) the dossier's own rule that splits subsidiaries into restricted vs "
        "unrestricted (perimeter rule): the numeric cut, which side of the cut is "
        "which, and the verbatim sentence. null if absent.\n"
        "Copy names and numbers exactly as printed.\n\nDOSSIER TEXT:\n"
        + _doc_text(doc_hash)
    )
    return generate(prompt, model=STRONG, schema=KYC_SCHEMA)


# ---------------------------------------------------------------- AUDIT

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "reclassifications": {
            "type": "array",
            "description": "ACCEPTED/FINAL reclassifications only",
            "items": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": ["string", "null"]},
                    "counterparty": {"type": ["string", "null"]},
                    "amount": {"type": ["number", "null"]},
                    "from_category": {"type": ["string", "null"]},
                    "to_category": {"type": "string", "enum": TAXONOMY},
                    "quote": {"type": "string"},
                },
                "required": ["to_category", "quote"],
            },
        },
        "rejected_reclassifications": {
            "type": "array",
            "description": "proposals the auditor REJECTED - must NOT be applied",
            "items": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": ["string", "null"]},
                    "counterparty": {"type": ["string", "null"]},
                    "quote": {"type": "string"},
                },
                "required": ["quote"],
            },
        },
        "exclusions": {
            "type": "array",
            "description": "txns excluded from the covenant period (cutoff/accrual rules)",
            "items": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": ["string", "null"]},
                    "counterparty": {"type": ["string", "null"]},
                    "amount": {"type": ["number", "null"]},
                    "quote": {"type": "string"},
                },
                "required": ["quote"],
            },
        },
        "amount_fills": {
            "type": "array",
            "description": "txns whose ledger amount is missing/wrong; true amount per the documents",
            "items": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": "string"},
                    "amount_usd": {"type": "number"},
                    "is_expense": {"type": "boolean"},
                    "quote": {"type": "string"},
                },
                "required": ["txn_id", "amount_usd", "is_expense", "quote"],
            },
        },
        "off_ledger": {
            "type": "array",
            "description": "obligations counting toward covenants with NO ledger row",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "category": {"type": "string", "enum": TAXONOMY},
                    "amount_usd": {"type": "number"},
                    "quote": {"type": "string"},
                },
                "required": ["description", "category", "amount_usd", "quote"],
            },
        },
        "fx_disclosures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "currency": {"type": "string"},
                    "foreign_amount": {"type": "number"},
                    "usd_amount": {"type": "number"},
                    "quote": {"type": "string"},
                },
                "required": ["currency", "foreign_amount", "usd_amount", "quote"],
            },
        },
        "stated_figures": {
            "type": "array",
            "description": ("aggregate figures the auditor STATES explicitly as totals "
                            "(revenue for the period, operating expenses, EBITDA, category "
                            "totals) - exact amounts with quotes; empty if none stated"),
            "items": {
                "type": "object",
                "properties": {
                    "metric_name": {"type": "string",
                                    "description": "auditor's own wording, e.g. 'выручка за период'"},
                    "amount_usd": {"type": "number"},
                    "quote": {"type": "string"},
                },
                "required": ["metric_name", "amount_usd", "quote"],
            },
        },
        "ebitda_addbacks": {
            "type": "array",
            "description": ("one-off items the auditor deems addable back to EBITDA, AFTER "
                            "applying any materiality floor the notes define - list only "
                            "QUALIFYING items"),
            "items": {
                "type": "object",
                "properties": {
                    "counterparty": {"type": ["string", "null"]},
                    "amount_usd": {"type": "number"},
                    "quote": {"type": "string"},
                },
                "required": ["amount_usd", "quote"],
            },
        },
    },
    "required": ["reclassifications", "rejected_reclassifications", "exclusions",
                 "amount_fills", "off_ledger", "fx_disclosures", "ebitda_addbacks",
                 "stated_figures"],
}


def extract_audit(doc_hash: str) -> dict:
    prompt = (
        "Extract covenant-relevant adjustments from this auditor/treasury document. Report:\n"
        "- accepted reclassifications (txn id if cited, else counterparty + amount);\n"
        "- proposals the auditor explicitly REJECTED;\n"
        "- period/cutoff exclusions;\n"
        "- transactions whose ledger amount is missing or wrong, with the true amount;\n"
        "- obligations that count toward covenants but have no ledger row (off-ledger);\n"
        "- implied FX rates from settlement notes (foreign amount + USD settled);\n"
        "- one-off EBITDA add-back items: if the notes define a materiality floor, apply it "
        "and list ONLY the qualifying items;\n"
        "- stated aggregate figures: every total the document states explicitly "
        "(revenue for the period, operating expenses, EBITDA, category totals).\n"
        "Every item needs a verbatim quote. Report ONLY what the document actually states - "
        "empty lists are the correct answer for absent sections.\n\nDOCUMENT TEXT:\n"
        + _doc_text(doc_hash)
    )
    return generate(prompt, model=STRONG, schema=AUDIT_SCHEMA)


# ---------------------------------------------------------------- COVENANTS

COMPONENT_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {"type": "array", "items": {"type": "string", "enum": TAXONOMY}},
        "include_subsidiary_transfers": {
            "type": "boolean",
            "description": ("true when this component measures total spending of a kind that "
                            "naturally includes asset transfers to subsidiaries (e.g. total "
                            "capital expenditures); false when the clause singles transfers "
                            "out or they are irrelevant"),
        },
        "period": {
            "type": ["array", "null"], "items": {"type": "string"},
            "description": "[start,end] ISO dates ONLY if the clause restricts this component "
                           "to a sub-period; null for the full covenant period",
        },
        "definition_quote": {"type": "string",
                             "description": "verbatim fragment justifying this component's contents"},
    },
    "required": ["categories", "include_subsidiary_transfers", "period", "definition_quote"],
}

COVENANT_SCHEMA = {
    "type": "object",
    "properties": {
        "clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause": {"type": "string"},
                    "title": {"type": "string"},
                    "components": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "definition": COMPONENT_SCHEMA,
                            },
                            "required": ["name", "definition"],
                        },
                    },
                    "formula": {"type": "string"},
                    "threshold": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["<=", ">="]},
                            "value": {"type": "number"},
                            "strict": {
                                "type": "boolean",
                                "description": ("true = value exactly AT the limit complies "
                                                "('не превышал X', 'не менее X'); false = at-limit "
                                                "already breaches ('менее X', 'более X')"),
                            },
                        },
                        "required": ["op", "value", "strict"],
                    },
                    "condition": {
                        "type": ["object", "null"],
                        "description": "springing trigger: limit applies ONLY when this holds",
                        "properties": {
                            "formula": {"type": "string"},
                            "op": {"type": "string", "enum": ["<", "<=", ">", ">="]},
                            "value": {"type": "number"},
                        },
                        "required": ["formula", "op", "value"],
                    },
                    "uses_ebitda_addbacks": {
                        "type": "boolean",
                        "description": ("true when the clause's metric is defined to include "
                                        "auditor-approved one-off add-backs to EBITDA/operating result"),
                    },
                    "unmodeled_condition": {
                        "type": ["string", "null"],
                        "description": "carve-out/rule this DSL cannot express; null if fully modeled",
                    },
                    "quote": {"type": "string"},
                },
                "required": ["clause", "title", "components", "formula", "threshold",
                             "condition", "uses_ebitda_addbacks", "unmodeled_condition", "quote"],
            },
        },
    },
    "required": ["clauses"],
}

COVENANT_PROMPT = """You are extracting financial covenants from a loan agreement into a
machine-readable spec executed by a deterministic engine. Produce EXACTLY these clauses
(the covenant clause keys of this agreement): {clause_keys}.

The engine works over a ledger of transactions, each labeled with ONE category from:
{taxonomy}

For each clause produce:
- components: named sums of transaction categories. For every component copy the verbatim
  definition fragment (definition_quote) that justifies its contents. Include ONLY the
  categories the clause or the agreement's own definitions actually name - never pad a
  metric with extra expense categories the text does not mention. Use RELATED_PARTY for
  payments to affiliated counterparties (affiliation per the KYC dossier, never per payment
  description). Use GROUP_CAPEX when the clause measures the GROUP's consolidated capital
  expenditures (from group financial statements rather than the borrower's ledger).
  Set include_subsidiary_transfers=true when the component measures total spending that by
  the clause's logic includes asset transfers to subsidiaries. Set "period" only when the
  clause tests a sub-period.
- formula: arithmetic over component names; max()/min() allowed; ratios as plain division.
- threshold: "<=" for caps, ">=" for floors; value as plain number ($3,000,000.00 ->
  3000000.00; 1.20x -> 1.20); strict=true when being exactly AT the limit still complies.
- condition: ONLY for springing covenants; null otherwise.
- uses_ebitda_addbacks: true when the metric's definition incorporates auditor-approved
  one-off add-backs. In that case reference the built-in variable `ebitda_addbacks`
  (the sum of qualifying add-back amounts, supplied by the engine) at the right place
  in the formula, e.g. "(revenue - opex + ebitda_addbacks) / revenue".
- unmodeled_condition: any rule you could NOT express in this DSL - never silently drop rules.
- quote: the verbatim clause text.

The engine automatically applies auditor reclassifications, cutoff exclusions,
missing-amount corrections, off-ledger additions and FX conversion - do not encode those
into the formula.

Example of ONE valid clause object (formula syntax: only component names, numbers,
+ - * / parentheses, max(), min() - no other functions, no comparison operators):
{{"clause": "6.1", "title": "Interest coverage",
  "components": [
    {{"name": "revenue", "definition": {{"categories": ["REVENUE"],
      "include_subsidiary_transfers": false, "period": null,
      "definition_quote": "Выручка за период..."}}}},
    {{"name": "opex", "definition": {{"categories": ["OPEX"],
      "include_subsidiary_transfers": false, "period": null,
      "definition_quote": "Операционные расходы..."}}}},
    {{"name": "interest", "definition": {{"categories": ["INTEREST"],
      "include_subsidiary_transfers": false, "period": null,
      "definition_quote": "Процентные расходы..."}}}}],
  "formula": "(revenue - opex) / interest",
  "threshold": {{"op": ">=", "value": 2.0, "strict": true}},
  "condition": null, "uses_ebitda_addbacks": false,
  "unmodeled_condition": null, "quote": "Пункт 6.1 ..."}}

AGREEMENT TEXT (full, including definitions):
{text}"""


def clause_keys_for(scenario_id: str) -> list:
    template = json.loads(TEMPLATE.read_text())
    return sorted(template["answers"][scenario_id].keys())


def _spec_signature(spec: dict) -> str:
    """Canonical form for structural comparison of two extraction passes."""
    canon = []
    for cl in sorted(spec["clauses"], key=lambda c: c["clause"]):
        canon.append({
            "clause": cl["clause"],
            "formula": "".join(cl["formula"].split()),
            "threshold": cl["threshold"],
            "condition": cl.get("condition"),
            "uses_ebitda_addbacks": cl.get("uses_ebitda_addbacks"),
            "components": sorted(
                (c["name"], tuple(sorted(c["definition"]["categories"])),
                 c["definition"].get("include_subsidiary_transfers"),
                 tuple(c["definition"].get("period") or ()))
                for c in cl["components"]),
        })
    return json.dumps(canon, sort_keys=True, default=str)


ARBITER_PROMPT = """Two independent machine extractions of the same loan-agreement covenants
disagree. Decide the CORRECT spec by reading the verbatim clause texts below. You may pick
one candidate or produce a corrected merge, but the output must follow the same schema and
cover exactly these clauses: {clause_keys}.

CANDIDATE A:
{a}

CANDIDATE B:
{b}

VERBATIM CLAUSE TEXTS (from candidate quotes):
{quotes}

Category vocabulary: {taxonomy}"""


def extract_covenants(doc_hash: str, scenario_id: str) -> dict:
    keys = clause_keys_for(scenario_id)
    text = _doc_text(doc_hash)
    base = COVENANT_PROMPT.format(
        clause_keys=keys, taxonomy=json.dumps(TAXONOMY), text=text)
    p1 = generate(base, model=STRONG, schema=COVENANT_SCHEMA, reasoning_effort="high")
    p2 = generate(base + "\n\n(Independent verification pass - read carefully.)",
                  model=STRONG, schema=COVENANT_SCHEMA, reasoning_effort="high")
    if _spec_signature(p1) == _spec_signature(p2):
        return p1
    quotes = "\n---\n".join(c["quote"] for c in p1["clauses"])
    print(f"  covenant passes disagree for {scenario_id} -> arbiter")
    arbiter = ARBITER_PROMPT.format(
        clause_keys=keys, a=json.dumps(p1, ensure_ascii=False),
        b=json.dumps(p2, ensure_ascii=False), quotes=quotes,
        taxonomy=json.dumps(TAXONOMY))
    return generate(arbiter, model=STRONG, schema=COVENANT_SCHEMA, reasoning_effort="high")


# ---------------------------------------------------------------- GROUP REPORTS

GROUP_LINK_SCHEMA = {
    "type": "object",
    "properties": {
        "consolidates_borrower": {"type": ["string", "null"],
                                  "description": "scenario_id whose borrower this group consolidates"},
        "evidence_quote": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["consolidates_borrower", "evidence_quote", "confidence"],
}

GROUP_SCHEMA = {
    "type": "object",
    "properties": {
        "capex_stated_directly": {
            "type": ["number", "null"],
            "description": "annual group capital expenditure if the report states it as a figure"},
        "ppe_begin": {"type": ["number", "null"]},
        "ppe_end": {"type": ["number", "null"]},
        "depreciation": {"type": ["number", "null"]},
        "disposals": {"type": ["number", "null"], "description": "0 if the report says none"},
        "quote": {"type": "string"},
    },
    "required": ["capex_stated_directly", "ppe_begin", "ppe_end",
                 "depreciation", "disposals", "quote"],
}


def extract_group(doc_hash: str, scenario_meta: dict) -> tuple:
    borrowers = {sid: [m["borrower_name"]] + m.get("name_variants", [])
                 for sid, m in scenario_meta.items()}
    link = generate(
        "This is a consolidated group financial report. Which of these borrowers does the "
        f"group consolidate as a subsidiary/segment, if any?\n{json.dumps(borrowers, ensure_ascii=False)}\n\n"
        "REPORT TEXT:\n" + _doc_text(doc_hash),
        model=CHEAP, schema=GROUP_LINK_SCHEMA)
    sid = link["consolidates_borrower"]
    if sid not in scenario_meta:
        return None, link
    g = generate(
        "From this consolidated report extract the group's annual capital expenditure if "
        "stated directly, and the property/plant/equipment movement (net book value begin/"
        "end of year, depreciation charge, disposals - 0 if stated as none). Use exact "
        "full-precision figures when both rounded and precise are present.\n\nREPORT TEXT:\n"
        + _doc_text(doc_hash),
        model=STRONG, schema=GROUP_SCHEMA)
    if g["capex_stated_directly"] is not None:
        capex = g["capex_stated_directly"]
    elif None not in (g["ppe_begin"], g["ppe_end"], g["depreciation"]):
        capex = round(g["ppe_end"] - g["ppe_begin"] + g["depreciation"]
                      + (g["disposals"] or 0), 2)
    else:
        capex = None
    return ({"doc": doc_hash, "group_capex": capex, **g} if capex is not None else None), link


# ---------------------------------------------------------------- shared / main

def _doc_text(doc_hash: str, max_chars: int = MAX_DOC_CHARS) -> str:
    return (PARSED / f"{doc_hash}.txt").read_text()[:max_chars]


def main() -> None:
    index = json.loads((REPO / "artifacts" / "doc_index.json").read_text())
    scenario_meta = json.loads((REPO / "artifacts" / "scenario_meta.json").read_text())
    out = {"kyc": {}, "audit": {}, "covenants": {}, "group": {}}

    for doc, meta in sorted(index.items()):
        sid, dt, auth = meta["scenario_id"], meta["doc_type"], meta["authority"]
        if dt == "kyc_dossier" and sid:
            print(f"KYC {sid} <- {doc}")
            out["kyc"][sid] = {"doc": doc, **extract_kyc(doc)}
        elif dt in ("audit_report", "treasury_memo") and sid and auth != "draft":
            print(f"AUDIT {sid} <- {doc}")
            out["audit"].setdefault(sid, []).append({"doc": doc, **extract_audit(doc)})
        elif dt == "loan_agreement" and auth == "active" and sid:
            print(f"COVENANTS {sid} <- {doc}")
            out["covenants"][sid] = {"doc": doc, **extract_covenants(doc, sid)}
        elif dt == "group_financials":
            g, link = extract_group(doc, scenario_meta)
            print(f"GROUP {doc} -> {link['consolidates_borrower']}")
            if g and link["consolidates_borrower"]:
                out["group"][link["consolidates_borrower"]] = g

    (REPO / "artifacts" / "facts.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False))
    for k in out:
        print(f"{k}: {sorted(out[k])}")


if __name__ == "__main__":
    main()
