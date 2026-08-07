#!/usr/bin/env python3
"""Borrower metadata from each ACTIVE loan agreement (one cheap call each).

Replaces regex company-name guessing, the categorization head-hack and the
hardcoded covenant year. Output: artifacts/scenario_meta.json
    {scenario_id: {borrower_name, name_variants, lender_name, industry,
                   covenant_period: {start, end}, ledger_currency}}
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import CHEAP, generate, pmap  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PARSED = REPO / "artifacts" / "parsed_docs"

SCHEMA = {
    "type": "object",
    "properties": {
        "borrower_name": {"type": "string", "description": "full legal name of the borrower"},
        "name_variants": {"type": "array", "items": {"type": "string"},
                          "description": "abbreviations / alternate spellings seen in the document"},
        "lender_name": {"type": "string"},
        "industry": {"type": "string", "description": "one line: what business the borrower runs"},
        "covenant_period": {
            "type": "object",
            "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
            "required": ["start", "end"],
            "description": "ISO dates of the covenant testing period",
        },
        "ledger_currency": {"type": "string", "description": "currency covenant amounts are stated in"},
    },
    "required": ["borrower_name", "name_variants", "lender_name", "industry",
                 "covenant_period", "ledger_currency"],
}

PROMPT = (
    "From this loan agreement extract: the BORROWER's full legal name (the company "
    "receiving the loan - not the lender bank), any name variants/abbreviations used, "
    "the lender's name, a one-line description of the borrower's industry/business, "
    "the covenant testing period (ISO dates), and the currency covenant amounts are "
    "stated in.\n\nAGREEMENT TEXT:\n"
)


def main() -> None:
    index = json.loads((REPO / "artifacts" / "doc_index.json").read_text())
    agreements = [(doc, m) for doc, m in sorted(index.items())
                  if m["doc_type"] == "loan_agreement" and m["authority"] == "active"
                  and m["scenario_id"]]

    def _one(item):
        doc, m = item
        text = (PARSED / f"{doc}.txt").read_text()[:30000]
        r = {"doc": doc, **generate(PROMPT + text, model=CHEAP, schema=SCHEMA)}
        print(m["scenario_id"], "->", r["borrower_name"])
        return m["scenario_id"], r

    meta = dict(pmap(_one, agreements))
    (REPO / "artifacts" / "scenario_meta.json").write_text(
        json.dumps(meta, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
