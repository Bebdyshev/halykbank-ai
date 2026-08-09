#!/usr/bin/env python3
"""S2 CLASSIFY: LLM document classification with deterministic hints.

The LLM decides doc_type / authority / owner; regex signals, account and txn
frequency counts are passed as HINTS in the prompt, never as decisions. A doc
classified `other` that references ledger txn/account ids is re-examined.

Output: artifacts/doc_index.json (same shape consumers expect).
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import CHEAP, STRONG, generate, pmap  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PARSED = REPO / "artifacts" / "parsed_docs"

# legacy signatures - HINTS ONLY (matched on whitespace-stripped text)
HINT_PATTERNS = {
    "superseded_banner": "НЕДЕЙСТВУЮЩАЯРЕДАКЦИЯ|НЕПРИМЕНЯЕТСЯ|SUPERSEDED",
    "executed_banner": "ИСПОЛНИТЕЛЬНЫЙЭКЗЕМПЛЯР|EXECUTEDCOPY",
    "draft_banner": "ПРОЕКТ|НЕЯВЛЯЕТСЯОКОНЧАТЕЛЬНОЙ|DRAFT",
    "kyc_marker": "Знайсвоегоклиента|KYC|финансовогомониторинга",
    "audit_marker": "АУДИТОРСКОЕДЕЛО|согласованныхпроцедур|Примечаниякфинансовойотчётности|Audit",
    "treasury_marker": "[Кк]азначейств|[Tt]reasury",
    "consolidated_marker": "CONSOLIDATED|консолидированн",
}

ACC_RE = re.compile(r"ACC-\d+(?!-)")
TXN_RE = re.compile(r"TXN-([A-Za-z0-9]+)-[A-Za-z0-9-]*?\d+")

SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": [
            "loan_agreement", "loan_amendment", "kyc_dossier", "audit_report",
            "treasury_memo", "group_financials", "other"]},
        "authority": {
            "type": "string",
            "enum": ["active", "superseded", "draft", "final", "n_a"],
            "description": ("active: the executed, currently-in-force edition; "
                            "superseded: an old edition explicitly replaced/void; "
                            "draft: interim/working version NOT the auditor's final position; "
                            "final: the auditor's/author's final authoritative version; "
                            "n_a: authority does not apply"),
        },
        "scenario_id": {"type": ["string", "null"],
                        "description": "borrower scenario this doc belongs to, null if none/unclear"},
        "attribution_basis": {"type": "string",
                              "description": "what identifies the owner (account id / company name / txn refs)"},
        "header_quote": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["doc_type", "authority", "scenario_id", "attribution_basis",
                 "header_quote", "confidence"],
}

PROMPT = """Classify this document from a corporate-lending dataset.

Document types:
- loan_agreement: a bank loan agreement with covenants. authority=active if it is the
  executed, currently-in-force edition; authority=superseded if it is explicitly marked
  as an old/replaced/void edition (banner text saying it no longer applies).
- loan_amendment: a document AMENDING an existing loan agreement («дополнительное
  соглашение», amendment agreement) - changes thresholds/definitions of the base
  agreement without replacing it. authority=active if executed and in force;
  authority=superseded if itself replaced/void.
- kyc_dossier: know-your-client / related-party dossier for one borrower.
- audit_report: auditor's report / notes to financial statements / agreed-upon-procedures
  report for one borrower. authority=final for the auditor's final position;
  authority=draft for interim/working documents explicitly marked as not final.
- treasury_memo: internal treasury/finance memo about ledger corrections.
- group_financials: consolidated financial statements / annual report of a HOLDING GROUP
  (a parent entity consolidating subsidiaries), not a single borrower's statements.
- other: anything else (policies, brand guides, minutes, ops updates, templates...).

Owner: the borrower scenario this document belongs to. Known scenarios and their bank
accounts: {scenario_accounts}. A document about borrower X belongs to X's scenario even
if it never quotes the account id. Use null when the document belongs to no single
borrower (e.g. a group-level report covering several, or generic material).

Deterministic hints (computed by code; advisory only - decide from the text):
- signature pattern hits: {hits}
- account-id mention counts: {acc_counts}
- transaction-id prefix counts: {txn_counts}
- pages needing OCR: {ocr_pages}

DOCUMENT TEXT (start):
{head}

DOCUMENT TEXT (end):
{tail}"""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def classify_doc(name: str, text: str, scenario_accounts: dict, ocr_pages,
                 model: str = CHEAP, head_chars: int = 4000):
    nospace = re.sub(r"\s+", "", text)
    hits = [k for k, pat in HINT_PATTERNS.items() if re.search(pat, nospace)]
    acc_counts = dict(Counter(ACC_RE.findall(nospace)).most_common(5))
    txn_counts = dict(Counter(TXN_RE.findall(nospace)).most_common(5))
    norm = normalize(text)
    prompt = PROMPT.format(
        scenario_accounts=json.dumps(scenario_accounts),
        hits=hits, acc_counts=acc_counts, txn_counts=txn_counts,
        ocr_pages=ocr_pages,
        head=norm[:head_chars], tail=norm[-1000:] if len(norm) > head_chars else "")
    return generate(prompt, model=model, schema=SCHEMA), acc_counts, txn_counts


def main() -> None:
    scenario_accounts = json.loads(
        (REPO / "artifacts" / "scenario_accounts.json").read_text())
    manifest = json.loads((REPO / "artifacts" / "doc_manifest.json").read_text())
    valid_sids = set(scenario_accounts)

    def _one(txt_file):
        name = txt_file.stem
        text = txt_file.read_text()
        ocr_pages = manifest.get(name, {}).get("ocr_needed_pages", [])

        result, acc_counts, txn_counts = classify_doc(
            name, text, scenario_accounts, ocr_pages)

        # retry with the strong model on shaky answers
        needs_retry = (
            result["confidence"] < 0.6
            or (result["doc_type"] != "other" and result["scenario_id"] is None
                and result["doc_type"] != "group_financials")
            or (result["scenario_id"] is not None
                and result["scenario_id"] not in valid_sids)
        )
        # safety net: an "other" doc referencing ledger ids deserves a hard look
        if result["doc_type"] == "other" and (acc_counts or txn_counts):
            needs_retry = True
        if needs_retry:
            result, _, _ = classify_doc(
                name, text, scenario_accounts, ocr_pages,
                model=STRONG, head_chars=12000)

        if result["scenario_id"] is not None and result["scenario_id"] not in valid_sids:
            result["scenario_id"] = None

        nospace = re.sub(r"\s+", "", text)
        return name, {
            **result,
            "account_id": scenario_accounts.get(result["scenario_id"]),
            "txn_refs": sorted(set(
                m.group(0) for m in re.finditer(r"TXN-[A-Za-z0-9-]+?\d+", nospace))),
            "ocr_needed_pages": ocr_pages,
        }

    index = dict(pmap(_one, sorted(PARSED.glob("*.txt"))))

    out = REPO / "artifacts" / "doc_index.json"
    out.write_text(json.dumps(index, indent=1, ensure_ascii=False))

    # EARLY DATASET SANITY CHECK - fail loudly at minute 8, not minute 40
    problems = []
    for sid in sorted(valid_sids):
        active = [k for k, v in index.items()
                  if v["doc_type"] == "loan_agreement" and v["authority"] == "active"
                  and v["scenario_id"] == sid]
        kyc = [k for k, v in index.items()
               if v["doc_type"] == "kyc_dossier" and v["scenario_id"] == sid]
        audit = [k for k, v in index.items()
                 if v["doc_type"] in ("audit_report", "treasury_memo")
                 and v["authority"] != "draft" and v["scenario_id"] == sid]
        if len(active) != 1:
            problems.append(f"{sid}: {len(active)} active agreements (expected 1): {active}")
        if not kyc:
            problems.append(f"{sid}: no KYC dossier")
        if not audit:
            problems.append(f"{sid}: no final audit/treasury doc")
    if problems:
        print("!" * 70)
        print("DATASET SANITY WARNINGS - inspect before trusting downstream stages:")
        for pr in problems:
            print("  !!", pr)
        print("!" * 70)
    else:
        print("dataset sanity check: OK (1 active agreement, KYC, audit per scenario)")

    by_type = Counter((v["doc_type"], v["authority"]) for v in index.values())
    print("doc types:", dict(by_type.most_common()))
    for t, a in [("loan_agreement", "active"), ("loan_agreement", "superseded"),
                 ("kyc_dossier", None), ("audit_report", "final"),
                 ("audit_report", "draft"), ("treasury_memo", None),
                 ("group_financials", None)]:
        docs = {k: v["scenario_id"] or "?" for k, v in index.items()
                if v["doc_type"] == t and (a is None or v["authority"] == a)}
        print(f"{t}/{a or '*'} ({len(docs)}): {docs}")


if __name__ == "__main__":
    main()
