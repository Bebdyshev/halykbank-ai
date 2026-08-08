#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import run_all
from compute import compute_cell

COV = {
    "components": {
        "tax_utilities": {"categories": ["TAX", "UTILITIES"]},
        "revenue": {"categories": ["REVENUE"]},
        "operating_expenses": {"categories": [
            "OPEX", "PAYROLL", "UTILITIES", "INSURANCE",
            "RENT_LEASE", "CONSULTING", "MARKETING"]},
    },
    "formula": "tax_utilities / (revenue - operating_expenses)",
    "threshold": {"op": "<=", "value": 0.30, "strict": True},
}

def test_narrows_broad_opex():
    fixed = run_all.narrow_negative_denominator(COV)
    assert fixed is not None
    assert fixed["components"]["operating_expenses"]["categories"] == ["OPEX"]
    # исходный ковенант не мутирован
    assert "PAYROLL" in COV["components"]["operating_expenses"]["categories"]

def test_leaves_narrow_opex_alone():
    cov = {**COV, "components": {**COV["components"],
           "operating_expenses": {"categories": ["OPEX"]}}}
    assert run_all.narrow_negative_denominator(cov) is None

def test_ignores_non_matching_formula():
    cov = {**COV, "formula": "tax_utilities / revenue"}
    assert run_all.narrow_negative_denominator(cov) is None

def test_repaired_covenant_computes_breach():
    # revenue 9.0M, OPEX 6.3M, PAYROLL 6.8M: широкий opex делает знаменатель
    # отрицательным; узкий даёт EBITDA 2.7M -> ratio 1.0/2.7 = 0.37 -> BREACH
    def row(txn, amt):
        return {"txn_id": txn, "date": "2025-06-01", "amount": amt,
                "currency": "USD"}
    rows = [row("TXN-X1-0001", 9_000_000.00), row("TXN-X1-0002", -6_300_000.00),
            row("TXN-X1-0003", -6_800_000.00), row("TXN-X1-0004", -1_000_000.00)]
    facts = {"categories": {"TXN-X1-0001": "REVENUE", "TXN-X1-0002": "OPEX",
                            "TXN-X1-0003": "PAYROLL", "TXN-X1-0004": "TAX"}}
    fixed = run_all.narrow_negative_denominator(COV)
    r = compute_cell(fixed, rows, facts)
    assert r["status"] == "BREACH"
    assert abs(r["actual"] - 0.37) < 0.01

if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); n += 1; print("PASS", name)
    print(f"{n}/{n} passed")
