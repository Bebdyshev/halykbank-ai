"""Unit tests for the covenant engine, built on facts that were manually
reverse-engineered from the practice set and shown to reproduce ground truth
exactly (B1, P8, P10 numbers match ground_truth.json to the cent)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from compute import compute_cell  # noqa: E402


def row(txn, amount, date="2025-06-15", currency="USD"):
    return {"txn_id": txn, "date": date, "amount": amount, "currency": currency}


# --- B1 6.1: interest coverage ratio, breached only via an audit reclass ----
def test_b1_icr_reclass_breach_with_evidence():
    rows = [
        row("TXN-B1-0001", 9_741_934.78),          # KEGOC revenue
        row("TXN-B1-0002", -6_166_592.66),         # plant O&M opex
        row("TXN-B1-0003", -1_540_833.29),         # Halyk interest
        row("TXN-B1-0020", -592_296.10),           # 'consulting' -> reclassed to interest
        row("TXN-B1-0999", -5_000_000.00),         # noise
    ]
    facts = {
        "categories": {
            "TXN-B1-0001": "REVENUE", "TXN-B1-0002": "OPEX",
            "TXN-B1-0003": "INTEREST", "TXN-B1-0020": "CONSULTING",
            "TXN-B1-0999": "NOISE",
        },
        "reclassifications": [{"txn_id": "TXN-B1-0020", "to_category": "INTEREST"}],
    }
    covenant = {
        "components": {
            "revenue": {"categories": ["REVENUE"]},
            "opex": {"categories": ["OPEX"]},
            "interest": {"categories": ["INTEREST"]},
        },
        "formula": "(revenue - opex) / interest",
        "threshold": {"op": ">=", "value": 2.00},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["status"] == "BREACH"
    assert r["actual"] == 1.68                     # matches ground truth
    assert r["evidence_txn_id"] == "TXN-B1-0020"   # reverting reclass flips verdict


# --- B1 6.2: max-of-lines cap, draft reclass must NOT be applied ------------
def test_b1_max_line_compliant():
    rows = [row("TXN-B1-0010", -1_284_663.42), row("TXN-B1-0011", -937_215.88)]
    facts = {"categories": {"TXN-B1-0010": "PAYROLL", "TXN-B1-0011": "UTILITIES"}}
    covenant = {
        "components": {
            "payroll": {"categories": ["PAYROLL"]},
            "utilities": {"categories": ["UTILITIES"]},
        },
        "formula": "max(payroll, utilities)",
        "threshold": {"op": "<=", "value": 1_500_000.00},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["status"] == "COMPLIANT"
    assert r["actual"] == 1_284_663.42
    assert r["evidence_txn_id"] is None            # never on COMPLIANT


# --- P8 6.1: ledger sum + PDF-only amount fill + off-ledger liability -------
def test_p8_personnel_obligations_offledger_breach():
    rows = [
        row("TXN-P8-0002", -2_418_663.27),
        row("TXN-P8-0031", None),                  # empty amount in ledger
    ]
    facts = {
        "categories": {"TXN-P8-0002": "PAYROLL", "TXN-P8-0031": "PAYROLL"},
        "amount_fills": {"TXN-P8-0031": -884_204.16},      # from audit note 8.1
        "off_ledger": [{"id": "severance", "category": "PAYROLL",
                        "amount": 918_447.52}],            # note 7.1, no ledger row
    }
    covenant = {
        "components": {"personnel": {"categories": ["PAYROLL"]}},
        "formula": "personnel",
        "threshold": {"op": "<=", "value": 4_000_000.00},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["status"] == "BREACH"
    assert r["actual"] == 4_221_314.95             # matches ground truth


# --- P8 6.3: related-party ratio; rounded actual EQUALS the limit but the ---
# --- status is decided on the unrounded value -------------------------------
def test_p8_related_party_ratio_breach_at_rounded_limit():
    rows = [
        row("TXN-P8-0016", -342_118.65),           # Syrdarya Capital (44.6% owned)
        row("TXN-P8-0001", 7_884_663.19),          # revenue
        row("TXN-P8-0020", -1_000_000.00),         # personnel co 36.2% - NOT related
    ]
    facts = {
        "categories": {"TXN-P8-0016": "RELATED_PARTY", "TXN-P8-0001": "REVENUE",
                       "TXN-P8-0020": "PAYROLL"},
        "related_party_txns": ["TXN-P8-0016"],
    }
    covenant = {
        "components": {
            "related": {"categories": ["RELATED_PARTY"]},
            "revenue": {"categories": ["REVENUE"]},
        },
        "formula": "related / revenue",
        "threshold": {"op": "<=", "value": 0.04},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["status"] == "BREACH"                 # 0.043391... > 0.04 unrounded
    assert r["actual"] == 0.04                     # 2dp-rounded, equals the limit
    assert r["evidence_txn_id"] == "TXN-P8-0016"   # dropping it flips the verdict


# --- P10 6.1: floor ratio saved by a reclass INTO the numerator -------------
def test_p10_insurance_ratio_reclass_saves_compliance():
    rows = [
        row("TXN-P10-0001", -248_663.19),
        row("TXN-P10-0012", -142_118.64),          # OPEX -> reclassed INSURANCE
        row("TXN-P10-0003", -1_204_663.28),
        row("TXN-P10-0004", -418_204.37),
    ]
    facts = {
        "categories": {"TXN-P10-0001": "INSURANCE", "TXN-P10-0012": "OPEX",
                       "TXN-P10-0003": "RENT", "TXN-P10-0004": "UTILITIES"},
        "reclassifications": [{"txn_id": "TXN-P10-0012", "to_category": "INSURANCE"}],
    }
    covenant = {
        "components": {
            "insurance": {"categories": ["INSURANCE"]},
            "rent": {"categories": ["RENT"]},
            "utilities": {"categories": ["UTILITIES"]},
        },
        "formula": "insurance / (rent + utilities)",
        "threshold": {"op": ">=", "value": 0.20},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["status"] == "COMPLIANT"
    assert r["actual"] == 0.24                     # matches ground truth
    assert r["evidence_txn_id"] is None


# --- springing covenant: limit applies only when the trigger fires ----------
def test_springing_condition_not_triggered_is_compliant():
    rows = [row("TXN-P3-0001", -1_800_000.00), row("TXN-P3-0002", 1_000_000.00),
            row("TXN-P3-0003", 3_000_000.00)]
    facts = {"categories": {"TXN-P3-0001": "DEBT", "TXN-P3-0002": "EBITDA",
                            "TXN-P3-0003": "FINANCING"}}
    covenant = {
        "components": {
            "debt": {"categories": ["DEBT"]},
            "ebitda": {"categories": ["EBITDA"]},
            "financing": {"categories": ["FINANCING"]},
        },
        "formula": "debt / ebitda",
        "threshold": {"op": "<=", "value": 1.70},
        "condition": {"formula": "financing", "op": ">", "value": 4_000_000.00},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["status"] == "COMPLIANT"              # 1.8 > 1.7 but trigger not fired
    assert r["actual"] == 1.8                      # actual is still the metric


# --- strict boundary: exactly at a cap is COMPLIANT -------------------------
def test_exactly_at_cap_is_compliant():
    rows = [row("TXN-P4-0001", -400_000.00), row("TXN-P4-0002", 10_000_000.00)]
    facts = {"categories": {"TXN-P4-0001": "RELATED_PARTY", "TXN-P4-0002": "REVENUE"}}
    covenant = {
        "components": {
            "related": {"categories": ["RELATED_PARTY"]},
            "revenue": {"categories": ["REVENUE"]},
        },
        "formula": "related / revenue",
        "threshold": {"op": "<=", "value": 0.04},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["status"] == "COMPLIANT"              # 0.04 == 0.04, strict '>'


# --- FX conversion at a document-derived rate -------------------------------
def test_fx_conversion():
    rows = [row("TXN-P3-0024", -612_884.25, currency="EUR"),
            row("TXN-P3-0030", -100_000.00)]
    facts = {
        "categories": {"TXN-P3-0024": "CAPEX", "TXN-P3-0030": "CAPEX"},
        "fx_rates": {"EUR": 1.16},
    }
    covenant = {
        "components": {"capex": {"categories": ["CAPEX"]}},
        "formula": "capex",
        "threshold": {"op": "<=", "value": 1_000_000.00},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["actual"] == round(612_884.25 * 1.16 + 100_000.00, 2)
    assert r["status"] == "COMPLIANT" if r["computation"]["raw_metric"] <= 1_000_000 else "BREACH"


# --- period window: only Q4 rows count when the component says so -----------
def test_quarter_window_component():
    rows = [row("TXN-B4-0001", 3_000_000.00, date="2025-03-10"),
            row("TXN-B4-0002", 2_000_000.00, date="2025-11-20")]
    facts = {"categories": {"TXN-B4-0001": "REVENUE", "TXN-B4-0002": "REVENUE"}}
    covenant = {
        "components": {"q4_revenue": {"categories": ["REVENUE"],
                                      "period": ["2025-10-01", "2025-12-31"]}},
        "formula": "q4_revenue",
        "threshold": {"op": ">=", "value": 3_500_000.00},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["actual"] == 2_000_000.00             # March row excluded
    assert r["status"] == "BREACH"


# --- auditor cutoff exclusion changes a ratio (P1 6.1 pattern) --------------
def test_cutoff_exclusion():
    rows = [
        row("TXN-P1-0010", -1_842_006.44),         # capex
        row("TXN-P1-0042", -3_104_882.61),         # opex
        row("TXN-P1-0014", -918_443.27),           # lease
        row("TXN-P1-0045", -612_884.19),           # services rendered 2026 -> excluded
    ]
    facts = {
        "categories": {"TXN-P1-0010": "CAPEX", "TXN-P1-0042": "OPEX",
                       "TXN-P1-0014": "LEASE", "TXN-P1-0045": "OPEX"},
        "exclusions": ["TXN-P1-0045"],
    }
    covenant = {
        "components": {
            "capex": {"categories": ["CAPEX"]},
            "opex": {"categories": ["OPEX"]},
            "lease": {"categories": ["LEASE"]},
        },
        "formula": "capex / (opex + lease)",
        "threshold": {"op": "<=", "value": 0.42},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["status"] == "BREACH"
    assert r["actual"] == 0.46                     # matches ground truth; with the
    #                                                excluded row it would be 0.40 COMPLIANT



# --- v2 engine features -----------------------------------------------------
def test_strict_false_at_limit_breaches():
    rows = [row("TXN-X1-0001", -400_000.00), row("TXN-X1-0002", 10_000_000.00)]
    facts = {"categories": {"TXN-X1-0001": "RELATED_PARTY", "TXN-X1-0002": "REVENUE"}}
    covenant = {
        "components": {
            "related": {"categories": ["RELATED_PARTY"]},
            "revenue": {"categories": ["REVENUE"]},
        },
        "formula": "related / revenue",
        "threshold": {"op": "<=", "value": 0.04, "strict": False},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["status"] == "BREACH"           # 0.04 == 0.04 but strict=False


def test_ebitda_addbacks_variable():
    rows = [row("TXN-X2-0001", 10_000_000.00), row("TXN-X2-0002", -8_000_000.00)]
    facts = {
        "categories": {"TXN-X2-0001": "REVENUE", "TXN-X2-0002": "OPEX"},
        "ebitda_addbacks": [500_000.00, 300_000.00],
    }
    covenant = {
        "components": {
            "revenue": {"categories": ["REVENUE"]},
            "opex": {"categories": ["OPEX"]},
        },
        "formula": "(revenue - opex + ebitda_addbacks) / revenue",
        "threshold": {"op": ">=", "value": 0.28, "strict": True},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["actual"] == 0.28               # (2.0M + 0.8M)/10M
    assert r["status"] == "COMPLIANT"        # strict floor: at-limit complies


def test_component_includes_subsidiary_transfers_only_when_flagged():
    rows = [row("TXN-X3-0001", -1_000_000.00), row("TXN-X3-0002", -400_000.00)]
    facts = {"categories": {"TXN-X3-0001": "CAPEX", "TXN-X3-0002": "SUBSIDIARY_TRANSFER"}}
    plain = {
        "components": {"capex": {"categories": ["CAPEX"]}},
        "formula": "capex",
        "threshold": {"op": "<=", "value": 2_000_000.00, "strict": True},
    }
    flagged = {
        "components": {"capex": {"categories": ["CAPEX"],
                                 "include_subsidiary_transfers": True}},
        "formula": "capex",
        "threshold": {"op": "<=", "value": 2_000_000.00, "strict": True},
    }
    assert compute_cell(plain, rows, facts)["actual"] == 1_000_000.00
    assert compute_cell(flagged, rows, facts)["actual"] == 1_400_000.00


def test_covenant_period_filters_rows():
    rows = [row("TXN-X4-0001", -500_000.00, date="2024-12-30"),
            row("TXN-X4-0002", -700_000.00, date="2025-03-01")]
    facts = {"categories": {"TXN-X4-0001": "CAPEX", "TXN-X4-0002": "CAPEX"}}
    covenant = {
        "components": {"capex": {"categories": ["CAPEX"]}},
        "formula": "capex",
        "threshold": {"op": "<=", "value": 5_000_000.00, "strict": True},
        "period": ["2025-01-01", "2025-12-31"],
    }
    r = compute_cell(covenant, rows, facts)
    assert r["actual"] == 700_000.00         # 2024 row filtered by covenant period




# --- composition map: explicit txn membership overrides categories ----------
def test_component_explicit_txn_ids():
    rows = [row("TXN-X5-0001", -1_000_000.00), row("TXN-X5-0002", -400_000.00),
            row("TXN-X5-0003", -250_000.00)]
    facts = {"categories": {"TXN-X5-0001": "OPEX", "TXN-X5-0002": "OPEX",
                            "TXN-X5-0003": "INSURANCE"},
             "off_ledger": [{"id": "sev", "category": "PAYROLL", "amount": 100_000.0}]}
    covenant = {
        "components": {"opex": {"categories": ["OPEX"],
                                "txn_ids": ["TXN-X5-0001", "TXN-X5-0003"],
                                "off_ledger_ids": ["sev"]}},
        "formula": "opex",
        "threshold": {"op": "<=", "value": 2_000_000.00, "strict": True},
    }
    r = compute_cell(covenant, rows, facts)
    # explicit list wins: 1,000,000 + 250,000 (insurance-labeled!) + 100,000 off-ledger
    assert r["actual"] == 1_350_000.00


def test_component_explicit_ids_respect_adjustments():
    rows = [row("TXN-X6-0001", None), row("TXN-X6-0002", -300_000.00)]
    facts = {"categories": {"TXN-X6-0001": "PAYROLL", "TXN-X6-0002": "PAYROLL"},
             "amount_fills": {"TXN-X6-0001": -700_000.00},
             "exclusions": ["TXN-X6-0002"]}
    covenant = {
        "components": {"p": {"categories": ["PAYROLL"],
                             "txn_ids": ["TXN-X6-0001", "TXN-X6-0002"],
                             "off_ledger_ids": []}},
        "formula": "p",
        "threshold": {"op": "<=", "value": 1_000_000.00, "strict": True},
    }
    r = compute_cell(covenant, rows, facts)
    assert r["actual"] == 700_000.00  # fill applied, excluded row still excluded



if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
