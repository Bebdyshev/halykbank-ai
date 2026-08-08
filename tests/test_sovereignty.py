#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import judge

TEMPLATE = {"answers": {"P9": {"6.1": {}}, "P7": {"6.1": {}}, "P1": {"6.1": {}}}}

def test_flip_reverted():
    before = {"P9": {"6.1": {"status": "BREACH", "actual": 0.22,
                             "evidence_txn_id": "TXN-P9-0025"}}}
    answers = {"P9": {"6.1": {"status": "COMPLIANT", "actual": 0.08,
                              "evidence_txn_id": None}}}
    report = []
    judge._apply_sovereignty(answers, before, TEMPLATE, report)
    assert answers["P9"]["6.1"] == before["P9"]["6.1"]
    assert any("judge_flip_reverted" in r for r in report)

def test_actual_refinement_also_reverted():
    before = {"P1": {"6.1": {"status": "BREACH", "actual": 1.18,
                             "evidence_txn_id": "TXN-P1-0001"}}}
    answers = {"P1": {"6.1": {"status": "BREACH", "actual": 1.16,
                              "evidence_txn_id": "TXN-P1-0001"}}}
    judge._apply_sovereignty(answers, before, TEMPLATE, [])
    assert answers["P1"]["6.1"]["actual"] == 1.18

def test_evidence_addition_kept():
    before = {"P1": {"6.1": {"status": "BREACH", "actual": 1.18,
                             "evidence_txn_id": None}}}
    answers = {"P1": {"6.1": {"status": "BREACH", "actual": 1.18,
                              "evidence_txn_id": "TXN-P1-0031"}}}
    judge._apply_sovereignty(answers, before, TEMPLATE, [])
    assert answers["P1"]["6.1"]["evidence_txn_id"] == "TXN-P1-0031"

def test_absent_cell_fill_stands():
    before = {"P7": {"6.1": None}}
    answers = {"P7": {"6.1": {"status": "COMPLIANT", "actual": 0.01,
                              "evidence_txn_id": None}}}
    judge._apply_sovereignty(answers, before, TEMPLATE, [])
    assert answers["P7"]["6.1"]["actual"] == 0.01

if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); n += 1; print("PASS", name)
    print(f"{n}/{n} passed")
