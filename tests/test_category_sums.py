#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import judge

def test_category_sums():
    rows = [
        {"txn_id": "T1", "amount": 100.0},
        {"txn_id": "T2", "amount": -40.0},
        {"txn_id": "T3", "amount": None},      # добирается из fills
        {"txn_id": "T4", "amount": -999.0},    # NOISE - исключён
        {"txn_id": "T5", "amount": -7.0},      # без категории - исключён
    ]
    sf = {"categories": {"T1": "REVENUE", "T2": "OPEX", "T3": "OPEX",
                         "T4": "NOISE"},
          "amount_fills": {"T3": -10.0}}
    sums = judge._category_sums(rows, sf)
    assert sums == {"OPEX": 50.0, "REVENUE": 100.0}

if __name__ == "__main__":
    test_category_sums(); print("PASS test_category_sums"); print("1/1 passed")
