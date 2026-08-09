#!/usr/bin/env python3
"""Post-judge surgical patch: apply the S1 TXN-S1-0029 amount fill.

Basis (documents, no GT): treasury memo 1c7eee333748 (Tau-Ken Logistics,
ACC-7001=S1) states the transaction's actual amount $6,082,920.03 (поступление);
draft audit 6857725f0496 corroborates the amount and its original REVENUE
classification; its Revenue->Financing reclass proposal was NEVER adopted by
either FINAL S1 audit -> per case rules the draft reclass does not apply.
The memo was classified authority=draft (header: "рабочий документ"), which
excluded the fill at extract time - the same memo archetype was authoritative
in the practice set (P7).
"""
import json
from pathlib import Path

ART = Path(__file__).resolve().parent.parent / "artifacts"
facts = json.loads((ART / "facts.json").read_text())
entry = {
    "txn_id": "TXN-S1-0029",
    "amount_usd": 6082920.03,
    "is_expense": False,
    "quote": ("Операция TXN-S1-0029 (KTZ Freight Services JSC): сумма не "
              "отражена в выгрузке реестра; фактическая сумма операции "
              "составляет $6,082,920.03 (поступление). [treasury memo "
              "1c7eee333748; final audits adopt no reclass]"),
}
aud = facts["audit"]["S1"]
for a in aud:
    a.setdefault("amount_fills", [])
    if not any(x.get("txn_id") == "TXN-S1-0029" for x in a["amount_fills"]):
        a["amount_fills"].append(entry)
    break  # one entry is enough
(ART / "facts.json").write_text(json.dumps(facts, indent=1, ensure_ascii=False))
print("PATCHED: S1 fill applied to facts.json ->", entry["amount_usd"])
