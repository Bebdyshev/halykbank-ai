#!/usr/bin/env python3
"""Orchestrator: LLM facts + LLM-categorized ledger -> deterministic compute ->
answers + flags. No pattern-based decisions live here anymore:

- categories, decoy status and KYC relationship kinds come from the
  categorization stage verbatim (validation may FLAG, never rewrite);
- audit adjustments resolve to txn ids deterministically only when unambiguous;
  ambiguity goes to a cheap LLM disambiguation; failure becomes a flag;
- covenant periods come from scenario_meta; perimeter/threshold rules from KYC;
- engine errors and missing facts become flags for the judge stage - a cell is
  never silently filled with a made-up verdict.

Outputs: artifacts/answers.json, artifacts/audit_trails.json, artifacts/flags.json
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compute import compute_cell  # noqa: E402
from llm import CHEAP, generate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ART = REPO / "artifacts"

RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "txn_id": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["txn_id", "reason"],
}


def parse_amounts(text: str) -> list:
    """All plausible monetary amounts in a quote, any common format."""
    out = []
    for m in re.finditer(r"\d[\d\s,.' ]*\d|\d", text or ""):
        raw = m.group(0).replace(" ", "").replace("'", "").replace(" ", "")
        if "," in raw and "." in raw:
            raw = raw.replace(",", "") if raw.rfind(".") > raw.rfind(",") \
                else raw.replace(".", "").replace(",", ".")
        elif "," in raw:
            head, _, tail = raw.rpartition(",")
            raw = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else raw.replace(",", "")
        try:
            val = float(raw)
            if val > 100:  # ignore percentages / clause numbers
                out.append(val)
        except ValueError:
            pass
    return out


def resolve_txn(entry, ledger_rows, sid, flags):
    """Audit items cite txn ids, or counterparty+amount. Deterministic match ->
    unambiguous hit; else LLM picks among candidates; else flag."""
    if entry.get("txn_id"):
        return entry["txn_id"]
    amounts = set(parse_amounts(entry.get("quote", "")))
    if entry.get("amount"):
        amounts.add(abs(entry["amount"]))
    cp = (entry.get("counterparty") or "").lower()

    candidates = []
    for row in ledger_rows:
        cp_hit = cp and any(tok in row["counterparty"].lower()
                            for tok in cp.split() if len(tok) > 3)
        amt_hit = row["amount"] is not None and any(
            abs(abs(row["amount"]) - a) < 0.01 for a in amounts)
        if amt_hit or (cp_hit and not amounts):
            candidates.append(row)
    exact = [r for r in candidates
             if not cp or any(tok in r["counterparty"].lower()
                              for tok in cp.split() if len(tok) > 3)]
    pool = exact or candidates
    if len(pool) == 1:
        return pool[0]["txn_id"]
    if pool:
        pick = generate(
            "An audit document adjusts one specific ledger transaction. Which candidate "
            f"row does it refer to?\nAUDIT ITEM: {json.dumps(entry, ensure_ascii=False)}\n"
            "CANDIDATES:\n" + "\n".join(
                f"{r['txn_id']} | {r['date']} | {r['counterparty']} | "
                f"{r['description']} | {r['amount']}" for r in pool[:10]) +
            "\nReturn null if none matches.",
            model=CHEAP, schema=RESOLVE_SCHEMA)
        if pick["txn_id"]:
            return pick["txn_id"]
    flags.append({"scenario": sid, "type": "unresolved_adjustment",
                  "detail": entry.get("quote", "")[:200]})
    return None


def build_scenario_facts(sid, ledger_rows, facts, categories, flags):
    kyc = facts["kyc"].get(sid, {})
    labels = categories.get(sid, {})

    cats, related_txns, kyc_linked = {}, [], []
    affiliate_names = set()
    threshold = kyc.get("related_party_threshold_pct")
    for o in kyc.get("ownership", []):
        if threshold is not None and o["pct"] >= threshold:
            affiliate_names.add(o["name"])

    # H4: the dossier's own numeric rules are LAW - enforced in code, overriding
    # any model intuition, with a flag for every forced change.
    def norm(s):
        import re as _re
        s = (s or "").lower()
        s = _re.sub(r"[«»\"'().,]", " ", s)
        s = _re.sub(r"\b(llp|jsc|llc|ltd|inc|тоо|ао|оао|зао|пао)\b", " ", s)
        return _re.sub(r"\s+", " ", s).strip()

    ownership_by_norm = {norm(o["name"]): o for o in kyc.get("ownership", [])}
    perim = kyc.get("perimeter_rule")
    subs_by_norm = {norm(s["name"]): s for s in kyc.get("subsidiaries", [])}

    def ownership_entry(row, lab):
        cand = lab.get("kyc_matched_name")
        if cand and norm(cand) in ownership_by_norm:
            return ownership_by_norm[norm(cand)]
        cp = norm(row["counterparty"])
        for n, o in ownership_by_norm.items():
            if n and (n in cp or cp in n):
                return o
        return None

    def sub_entry(row, lab):
        cand = lab.get("kyc_matched_name")
        if cand and norm(cand) in subs_by_norm:
            return subs_by_norm[norm(cand)]
        cp = norm(row["counterparty"])
        for n, s in subs_by_norm.items():
            if n and (n in cp or cp in n):
                return s
        return None

    for row in ledger_rows:
        txn = row["txn_id"]
        lab = labels.get(txn)
        if lab is None:
            flags.append({"scenario": sid, "type": "unlabeled_txn", "detail": txn})
            cats[txn] = "OPEX"
            continue
        cats[txn] = lab["category"]

        own = ownership_entry(row, lab)
        is_payment = (row["amount"] or 0) < 0
        if own is not None and threshold is not None and lab["category"] != "NOISE":
            if own["pct"] >= threshold and is_payment                     and lab["category"] != "RELATED_PARTY":
                flags.append({"scenario": sid, "type": "kyc_rule_enforced",
                              "detail": f"{txn}: {own['name']} {own['pct']}% >= "
                                        f"{threshold}% -> RELATED_PARTY"})
                cats[txn] = "RELATED_PARTY"
            elif own["pct"] < threshold and lab["category"] == "RELATED_PARTY":
                flags.append({"scenario": sid, "type": "kyc_rule_enforced",
                              "detail": f"{txn}: {own['name']} {own['pct']}% < "
                                        f"{threshold}% -> not related (CONSULTING)"})
                cats[txn] = "CONSULTING"
        if cats[txn] == "RELATED_PARTY":
            if own is None:
                flags.append({"scenario": sid, "type": "related_party_unverified",
                              "detail": f"{txn}: {lab.get('kyc_matched_name')}"})
            related_txns.append(txn)

        # subsidiary perimeter: transfers to RESTRICTED subs are excluded from
        # the transfers metric (they remain plain capex by nature)
        if cats[txn] == "SUBSIDIARY_TRANSFER":
            sub = sub_entry(row, lab)
            if sub is not None and perim:
                above = sub["pledged_pct"] >= perim["cut_pct"]
                treatment = (perim["above_or_equal_treatment"] if above
                             else perim["below_treatment"])
                if treatment == "restricted":
                    flags.append({"scenario": sid, "type": "kyc_rule_enforced",
                                  "detail": f"{txn}: {sub['name']} pledged "
                                            f"{sub['pledged_pct']}% -> restricted, "
                                            "transfer excluded (CAPEX)"})
                    cats[txn] = "CAPEX"
                else:
                    kyc_linked.append(txn)
            else:
                kyc_linked.append(txn)

    if threshold is None and related_txns:
        flags.append({"scenario": sid, "type": "kyc_threshold_missing",
                      "detail": "related-party txns labeled but no threshold extracted"})

    merged = {
        "categories": cats,
        "related_party_txns": related_txns + kyc_linked,
        "reclassifications": [],
        "exclusions": [],
        "amount_fills": {},
        "off_ledger": [],
        "fx_rates": {},
        "ebitda_addbacks": [],
    }
    for a in facts["audit"].get(sid, []):
        for r in a.get("reclassifications", []):
            txn = resolve_txn(r, ledger_rows, sid, flags)
            if txn:
                merged["reclassifications"].append(
                    {"txn_id": txn, "to_category": r["to_category"]})
        for e in a.get("exclusions", []):
            txn = resolve_txn(e, ledger_rows, sid, flags)
            if txn:
                merged["exclusions"].append(txn)
        for f in a.get("amount_fills", []):
            amt = -abs(f["amount_usd"]) if f["is_expense"] else abs(f["amount_usd"])
            merged["amount_fills"][f["txn_id"]] = amt
        for o in a.get("off_ledger", []):
            merged["off_ledger"].append(
                {"id": o["description"], "category": o["category"],
                 "amount": abs(o["amount_usd"])})
        for fx in a.get("fx_disclosures", []):
            if fx.get("foreign_amount"):
                merged["fx_rates"][fx["currency"]] = fx["usd_amount"] / fx["foreign_amount"]
        merged["ebitda_addbacks"] += [
            abs(x["amount_usd"]) for x in a.get("ebitda_addbacks", [])]

    group = facts.get("group", {}).get(sid)
    if group and group.get("group_capex") is not None:
        merged["off_ledger"].append({
            "id": f"group_capex:{group['doc']}", "category": "GROUP_CAPEX",
            "amount": group["group_capex"]})

    # unusable rows the engine will drop -> surface them
    for row in ledger_rows:
        if row["amount"] is None and row["txn_id"] not in merged["amount_fills"]:
            flags.append({"scenario": sid, "type": "amount_missing_unfilled",
                          "detail": row["txn_id"]})
        if row.get("currency", "USD") != "USD" \
                and row["currency"] not in merged["fx_rates"] \
                and cats.get(row["txn_id"]) != "NOISE":
            flags.append({"scenario": sid, "type": "fx_rate_missing",
                          "detail": f"{row['txn_id']} {row['currency']}"})
    return merged


def main() -> None:
    ledger = json.loads((ART / "ledger_real.json").read_text())
    facts = json.loads((ART / "facts.json").read_text())
    categories = json.loads((ART / "categorized_ledger.json").read_text())
    meta = json.loads((ART / "scenario_meta.json").read_text())
    comp_path = ART / "composition.json"
    composition = json.loads(comp_path.read_text()) if comp_path.exists() else {}
    if (ART / "composition_flags.json").exists():
        comp_flags = json.loads((ART / "composition_flags.json").read_text())
    else:
        comp_flags = []

    answers, trails, flags = {}, {}, list(comp_flags)
    for sid in sorted(ledger):
        sf = build_scenario_facts(sid, ledger[sid], facts, categories, flags)
        cov = facts["covenants"].get(sid)
        if not cov:
            flags.append({"scenario": sid, "type": "covenants_missing", "detail": ""})
            continue
        period = (meta.get(sid, {}).get("covenant_period") or {})
        answers[sid], trails[sid] = {}, {"facts": sf, "cells": {}}
        for clause_spec in cov["clauses"]:
            clause = clause_spec["clause"]
            comp_defs = {c["name"]: dict(c["definition"])
                         for c in clause_spec["components"]}
            # auditor-specific composition map: explicit membership overrides
            # the category-based definition (only confident entries)
            cmap = composition.get(sid, {}).get(clause, {})
            for name, m in cmap.items():
                if name not in comp_defs or not m.get("confident", True):
                    continue
                # ADOPTION GATE: an explicit membership list is just another
                # opinion unless the auditor's own stated figure confirms it
                # to the cent. Unconfirmed divergence goes to the judge.
                if m.get("stated_confirmed"):
                    valid = {r["txn_id"] for r in ledger[sid]}
                    ids = [t for t in m["txn_ids"] if t in valid]
                    if len(ids) < len(m["txn_ids"]):
                        flags.append({"scenario": sid, "type": "composition_bad_txn",
                                      "detail": f"{clause}/{name}: "
                                                f"{set(m['txn_ids']) - valid}"})
                    comp_defs[name]["txn_ids"] = ids
                    comp_defs[name]["off_ledger_ids"] = m.get("off_ledger_ids", [])
                else:
                    flags.append({"scenario": sid, "type": "composition_divergence",
                                  "detail": f"{clause}/{name}: composed "
                                            f"{m.get('composed_sum')} unconfirmed "
                                            "(category-based value kept)"})
            covenant = {
                "components": comp_defs,
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
                              "detail": f"{clause}: {e}"})
                continue  # cell left absent -> judge/fallback must produce it
            if clause_spec.get("unmodeled_condition"):
                r["computation"]["unmodeled_condition"] = clause_spec["unmodeled_condition"]
                flags.append({"scenario": sid, "type": "unmodeled_condition",
                              "detail": f"{clause}: {clause_spec['unmodeled_condition'][:150]}"})
            answers[sid][clause] = {k: r[k] for k in ("status", "actual", "evidence_txn_id")}
            trails[sid]["cells"][clause] = {**r["computation"],
                                            "quote": clause_spec.get("quote", "")}

    (ART / "answers.json").write_text(json.dumps(answers, indent=1, ensure_ascii=False))
    (ART / "audit_trails.json").write_text(json.dumps(trails, indent=1, ensure_ascii=False))
    (ART / "flags.json").write_text(json.dumps(flags, indent=1, ensure_ascii=False))
    n_cells = sum(len(v) for v in answers.values())
    print(f"computed {n_cells} cells for {len(answers)} scenarios; {len(flags)} flags")
    for f in flags:
        print(f"  FLAG [{f['scenario']}] {f['type']}: {f['detail'][:100]}")


if __name__ == "__main__":
    main()
