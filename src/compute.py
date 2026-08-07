#!/usr/bin/env python3
"""S5 COMPUTE: deterministic covenant engine. Zero LLM.

Covenant spec (produced by the LLM extraction stage):
{
  "components": {name: {"categories": [...],
                        "include_subsidiary_transfers": bool,   # optional
                        "period": ["start","end"] | null}},
  "formula": "(revenue - opex + ebitda_addbacks) / interest",
  "threshold": {"op": "<=", "value": 3.0, "strict": true},
      # strict=true: value exactly AT the limit complies ("не превышал")
      # strict=false: at-limit already breaches ("менее X")
  "condition": {...} | absent,      # springing trigger
  "period": ["start","end"],        # covenant testing period (from the agreement)
}

Facts: categories, fx_rates, amount_fills, exclusions, reclassifications,
off_ledger, related_party_txns, ebitda_addbacks (list of amounts - exposed to
formulas as the built-in variable `ebitda_addbacks`).

Evidence = counterfactual flip test over FACT-LINKED txns only (reclassified,
related-party/KYC-linked, corrected) - never ordinary contributors.
"""
import ast
import operator


class NegativeDenominator(Exception):
    """A ratio's denominator evaluated to <= 0 - the metric sign is meaningless
    and a signed comparison would silently auto-pass caps. Callers must treat
    the cell as broken, not COMPLIANT."""


class EmptyComponent(Exception):
    """A component resolved to no categories and no explicit txn list."""

DEFAULT_PERIOD = ("0000-01-01", "9999-12-31")  # fallback: no date filtering

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _check_denominators(node, names):
    """Walk the AST; any division's right side must evaluate > 0."""
    if isinstance(node, ast.Expression):
        _check_denominators(node.body, names)
    elif isinstance(node, ast.BinOp):
        _check_denominators(node.left, names)
        _check_denominators(node.right, names)
        if isinstance(node.op, ast.Div):
            d = _eval_expr(node.right, names)
            if d <= 0:
                raise NegativeDenominator(
                    f"denominator evaluates to {d}")
    elif isinstance(node, ast.UnaryOp):
        _check_denominators(node.operand, names)
    elif isinstance(node, ast.Call):
        for a in node.args:
            _check_denominators(a, names)


def _eval_expr(node, names):
    if isinstance(node, ast.Expression):
        return _eval_expr(node.body, names)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        return names[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_expr(node.left, names), _eval_expr(node.right, names))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_expr(node.operand, names))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("max", "min"):
        fn = max if node.func.id == "max" else min
        return fn(_eval_expr(a, names) for a in node.args)
    raise ValueError(f"disallowed expression node: {ast.dump(node)}")


def eval_formula(formula: str, names: dict) -> float:
    tree = ast.parse(formula, mode="eval")
    _check_denominators(tree, names)
    return _eval_expr(tree, names)


def effective_rows(rows, facts, revert_txn=None):
    """Apply facts to raw ledger rows -> list of (txn_id, date, usd_abs, category).

    revert_txn: undo the fact attached to this txn (flip-test): a reclassified
    txn goes back to its base category; any other fact-linked txn is dropped.
    """
    reclass = {r["txn_id"]: r["to_category"] for r in facts.get("reclassifications", [])}
    fills = facts.get("amount_fills", {})
    excl = set(facts.get("exclusions", []))
    fx = facts.get("fx_rates", {})
    cats = facts.get("categories", {})

    out = []
    for row in rows:
        txn = row["txn_id"]
        if txn in excl and txn != revert_txn:
            continue  # reverting an exclusion puts the row back (flip test)
        amount = row["amount"] if row["amount"] is not None else fills.get(txn)
        if amount is None:
            continue  # empty cell with no documented fill: unusable (flagged upstream)
        if row.get("currency", "USD") != "USD":
            rate = fx.get(row["currency"])
            if rate is None:
                continue  # no documented rate (flagged upstream)
            amount *= rate
        category = cats.get(txn)
        if txn in reclass and txn != revert_txn:
            category = reclass[txn]
        if txn == revert_txn and txn not in reclass and txn not in excl:
            continue  # revert an inclusion-type fact by dropping the txn
        out.append((txn, row["date"], abs(amount), category))
    return out


def component_value(eff_rows, spec, off_ledger, covenant_period):
    if not spec.get("categories") and spec.get("txn_ids") is None:
        raise EmptyComponent("component has neither categories nor txn_ids")
    start, end = spec.get("period") or covenant_period
    if spec.get("txn_ids") is not None:
        # explicit membership (auditor-specific composition map): sum exactly
        # these rows (post-adjustment) + explicitly named off-ledger items
        wanted = set(spec["txn_ids"])
        total = sum(a for (t, d, a, _) in eff_rows
                    if t in wanted and start <= d <= end)
        off_wanted = set(spec.get("off_ledger_ids") or [])
        total += sum(o["amount"] for o in off_ledger if o.get("id") in off_wanted)
        return total
    cats = set(spec["categories"])
    if spec.get("include_subsidiary_transfers"):
        cats |= {"SUBSIDIARY_TRANSFER"}
    total = sum(a for (_, d, a, c) in eff_rows if c in cats and start <= d <= end)
    total += sum(o["amount"] for o in off_ledger if o["category"] in cats)
    return total


def _names(covenant, rows, facts, revert_txn=None):
    eff = effective_rows(rows, facts, revert_txn=revert_txn)
    off = facts.get("off_ledger", [])
    period = tuple(covenant.get("period") or DEFAULT_PERIOD)
    names = {name: component_value(eff, spec, off, period)
             for name, spec in covenant["components"].items()}
    names.setdefault("ebitda_addbacks",
                     sum(facts.get("ebitda_addbacks", [])))
    return names


def _metric(covenant, rows, facts, revert_txn=None):
    names = _names(covenant, rows, facts, revert_txn)
    return eval_formula(covenant["formula"], names), names


def _status(value, covenant, condition_value=None) -> str:
    cond = covenant.get("condition")
    if cond is not None:
        if not _compare(condition_value, cond["op"], cond["value"]):
            return "COMPLIANT"  # limit not in effect; actual still reported
    th = covenant["threshold"]
    strict = th.get("strict", True)
    if th["op"] == "<=":
        breach = value > th["value"] if strict else value >= th["value"]
    elif th["op"] == ">=":
        breach = value < th["value"] if strict else value <= th["value"]
    else:
        raise ValueError(th["op"])
    return "BREACH" if breach else "COMPLIANT"


def _compare(value, op, limit):
    return {"<": operator.lt, "<=": operator.le,
            ">": operator.gt, ">=": operator.ge}[op](value, limit)


def _condition_value(covenant, rows, facts, revert_txn=None):
    if covenant.get("condition") is None:
        return None
    names = _names(covenant, rows, facts, revert_txn)
    return eval_formula(covenant["condition"]["formula"], names)


def compute_cell(covenant, rows, facts) -> dict:
    value, components = _metric(covenant, rows, facts)
    status = _status(value, covenant, _condition_value(covenant, rows, facts))

    evidence = None
    candidates = []
    if status == "BREACH":
        reclassed = [r["txn_id"] for r in facts.get("reclassifications", [])]
        related = [t for t in facts.get("related_party_txns", []) if t not in reclassed]
        filled = [t for t in facts.get("amount_fills", {}) if t not in reclassed + related]
        excluded = [t for t in facts.get("exclusions", [])
                    if t not in reclassed + related + filled]
        candidates = reclassed + related + filled + excluded
        for candidate in candidates:
            v2, _ = _metric(covenant, rows, facts, revert_txn=candidate)
            s2 = _status(v2, covenant,
                         _condition_value(covenant, rows, facts, revert_txn=candidate))
            if s2 != status:
                evidence = candidate
                break

    return {
        "status": status,
        "actual": round(abs(value), 2),
        "evidence_txn_id": evidence,
        "computation": {
            "raw_metric": value,
            "components": components,
            "threshold": covenant["threshold"],
            "evidence_candidates": candidates,
            "margin_pct": (
                None if covenant["threshold"]["value"] == 0
                else (value - covenant["threshold"]["value"])
                / abs(covenant["threshold"]["value"]) * 100
            ),
        },
    }
