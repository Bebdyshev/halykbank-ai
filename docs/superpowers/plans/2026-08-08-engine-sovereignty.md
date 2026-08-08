# Engine Sovereignty + Denominator Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать ±2-балльную дисперсию боевого прогона: движковые ответы неприкосновенны, отрицательный знаменатель чинится сужением до OPEX, решатель детерминированнее.

**Architecture:** Три точечных изменения в существующих файлах (judge.py, run_all.py) без новых модулей и без изменения промптов извлечения/классификации (кэш-дисциплина дня Х). Каждое изменение — чистая функция + вызов, покрытая юнит-тестом.

**Tech Stack:** Python 3.12, стоящий venv `.venv`, тесты — самодельный runner `tests/test_compute.py` (без pytest).

## Global Constraints

- НЕ менять промпты/схемы стадий ingest/classify/scenario_meta/extract/categorize/composition (инвалидация кэша запрещена; исключение — промпт решателя, его кэш крошечный).
- GT (`case-related-docs/ground_truth.json`, `scripts/score.py`) трогать ТОЛЬКО в Task 5, один замер, с явным объявлением пользователю.
- После каждого таска: `.venv/bin/python tests/test_compute.py` → все зелёные; смоук `import judge, run_all`.
- Коммит после каждого таска.

---

### Task 1: Замок статусов (engine sovereignty) в judge.py

**Files:**
- Modify: `src/judge.py` (блок «never-worse invariant», ~строка 545)
- Test: `tests/test_sovereignty.py` (новый)

**Interfaces:**
- Produces: `judge._apply_sovereignty(answers, answers_before, template, report) -> None` (мутирует answers на месте; вызывается в `judge.main` вместо never-worse блока).

- [ ] **Step 1: Написать падающий тест**

```python
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
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/python tests/test_sovereignty.py`
Expected: `AttributeError: module 'judge' has no attribute '_apply_sovereignty'`

- [ ] **Step 3: Реализация — заменить never-worse блок в `judge.main` на вызов новой функции**

В `src/judge.py` добавить функцию (перед `def main`):

```python
def _apply_sovereignty(answers, answers_before, template, report):
    """Engine sovereignty: a cell the deterministic engine computed is FINAL.
    Measured across 9 disclosed GT runs: judge/solver status flips of computed
    cells were wrong 6/6, and even actual 'refinements' hurt (P2 1.18->1.16).
    The only accepted post-judge improvement is ADDING evidence where the
    engine had none. Absent cells keep judge repairs / solver fills."""
    for sid, clauses in template["answers"].items():
        for clause in clauses:
            before = answers_before.get(sid, {}).get(clause)
            after = answers.get(sid, {}).get(clause)
            if before is None or after == before:
                continue
            restored = dict(before)
            if restored.get("evidence_txn_id") is None and after \
                    and after.get("evidence_txn_id"):
                restored["evidence_txn_id"] = after["evidence_txn_id"]
            answers.setdefault(sid, {})[clause] = restored
            report.append(
                f"{sid}/{clause}: judge_flip_reverted -> engine answer restored "
                f"(judged was {after and after.get('status')}/"
                f"{after and after.get('actual')})")
```

Существующий блок в `main`:

```python
    # ---- never-worse invariant: a cell the judge's interventions broke gets
    # its pre-judge answer back, not a solver guess
    for sid, clauses in template["answers"].items():
        for clause in clauses:
            if answers.get(sid, {}).get(clause) is None \
                    and answers_before.get(sid, {}).get(clause) is not None:
                answers.setdefault(sid, {})[clause] = answers_before[sid][clause]
                report.append(f"{sid}/{clause}: restored pre-judge answer "
                              "(judge intervention broke the cell)")
```

заменить на:

```python
    # ---- engine sovereignty (see _apply_sovereignty docstring)
    _apply_sovereignty(answers, answers_before, template, report)
```

(Блок «fill any still-missing cells with the fallback solver» ниже НЕ трогать —
он идёт ПОСЛЕ и заполняет только пустые; замок его результатов не касается,
т.к. для них `before is None`.)

- [ ] **Step 4: Тесты зелёные**

Run: `.venv/bin/python tests/test_sovereignty.py && .venv/bin/python tests/test_compute.py | tail -1 && .venv/bin/python -c "import sys; sys.path.insert(0,'src'); import judge; print('import ok')"`
Expected: `4/4 passed`, `19/19 passed`, `import ok`

- [ ] **Step 5: Commit**

```bash
git add tests/test_sovereignty.py src/judge.py
git commit -m "Engine sovereignty: computed cells are final; judge may only add evidence or fill absent cells"
```

---

### Task 2: Ремонт отрицательного знаменателя в run_all.py

**Files:**
- Modify: `src/run_all.py` (except-блок `NegativeDenominator` в `main`, ~строка 380; новая функция перед `build_covenant`)
- Test: `tests/test_denominator_repair.py` (новый)

**Interfaces:**
- Produces: `run_all.narrow_negative_denominator(covenant) -> dict | None` (чистая: возвращает починенную копию ковенанта или None, если паттерн неприменим).

- [ ] **Step 1: Написать падающий тест**

```python
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
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/python tests/test_denominator_repair.py`
Expected: `AttributeError: module 'run_all' has no attribute 'narrow_negative_denominator'`

- [ ] **Step 3: Реализация**

В `src/run_all.py` перед `def build_covenant` добавить:

```python
def narrow_negative_denominator(covenant):
    """A ratio X / (A - B) went negative because B ('operating expenses') was
    extracted BROADLY (OPEX + PAYROLL + ...). Our taxonomy already carves
    payroll/insurance/rent out of OPEX, so the agreement's «Операционные
    расходы» is the narrow OPEX category (confirmed: P7 GT, P6 relabels).
    Returns a repaired covenant copy, or None when the pattern doesn't apply.
    Only ever called when the cell would otherwise stay absent."""
    m = re.fullmatch(r"\s*(.+?)\s*/\s*\(\s*(\w+)\s*-\s*(\w+)\s*\)\s*",
                     covenant["formula"])
    if not m:
        return None
    b_name = m.group(3)
    b = covenant["components"].get(b_name)
    if b is None:
        return None
    cats = set(b.get("categories") or [])
    if "OPEX" not in cats or cats == {"OPEX"}:
        return None
    return {**covenant,
            "components": {**covenant["components"],
                           b_name: {**b, "categories": ["OPEX"]}}}
```

Except-блок в `main`:

```python
            except compute.NegativeDenominator as e:
                flags.append({"scenario": sid, "type": "negative_denominator",
                              "detail": f"{clause}: {e} - spec is broken, a "
                                        "signed compare would silently pass"})
                continue  # cell left absent -> judge/fallback must produce it
```

заменить на:

```python
            except compute.NegativeDenominator as e:
                repaired = narrow_negative_denominator(covenant)
                r = None
                if repaired is not None:
                    try:
                        r = compute_cell(repaired, ledger[sid], sf)
                        flags.append({
                            "scenario": sid,
                            "type": "denominator_narrowed_to_opex",
                            "detail": f"{clause}: broad opex made the "
                                      f"denominator negative ({e}); narrowed "
                                      "to OPEX-only and recomputed"})
                    except Exception:  # noqa: BLE001
                        r = None
                if r is None:
                    flags.append({"scenario": sid, "type": "negative_denominator",
                                  "detail": f"{clause}: {e} - spec is broken, a "
                                            "signed compare would silently pass"})
                    continue  # cell left absent -> judge/fallback must produce it
```

(Ниже по коду `r` используется как обычно — после ремонта поток просто
продолжается с починенным результатом.)

- [ ] **Step 4: Тесты зелёные**

Run: `.venv/bin/python tests/test_denominator_repair.py && .venv/bin/python tests/test_compute.py | tail -1 && .venv/bin/python -c "import sys; sys.path.insert(0,'src'); import run_all; print('import ok')"`
Expected: `4/4 passed`, `19/19 passed`, `import ok`

- [ ] **Step 5: Commit**

```bash
git add tests/test_denominator_repair.py src/run_all.py
git commit -m "Repair negative ratio denominators by narrowing broad opex to OPEX-only"
```

---

### Task 3: Усиление решателя (5 сэмплов + готовые суммы)

**Files:**
- Modify: `src/judge.py` (`fallback_solve_voted` сигнатура; `fallback_solve` промпт; новая функция `_category_sums`)
- Test: `tests/test_category_sums.py` (новый)

**Interfaces:**
- Produces: `judge._category_sums(rows, facts_sf) -> dict[str, float]` (суммы |amount| по категориям, NOISE исключён, пустые суммы добраны из amount_fills).

- [ ] **Step 1: Написать падающий тест**

```python
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
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/python tests/test_category_sums.py`
Expected: `AttributeError: module 'judge' has no attribute '_category_sums'`

- [ ] **Step 3: Реализация**

В `src/judge.py` перед `fallback_solve` добавить:

```python
def _category_sums(rows, facts_sf):
    """Deterministic per-category |amount| totals (fills applied, NOISE and
    unlabeled rows excluded) - handed to the fallback solver so the LLM picks
    the formula composition but never does freehand arithmetic."""
    sums = {}
    fills = facts_sf.get("amount_fills", {})
    for r in rows:
        cat = facts_sf.get("categories", {}).get(r["txn_id"])
        amt = r["amount"] if r.get("amount") is not None else fills.get(r["txn_id"])
        if cat and cat != "NOISE" and amt is not None:
            sums[cat] = round(sums.get(cat, 0.0) + abs(amt), 2)
    return sums
```

В `fallback_solve` в промпт (после строки с ADJUSTMENT FACTS) добавить блок сумм:

```python
    prompt = (
        f"(independent attempt #{sample + 1}) "
        "Independently compute this covenant cell end-to-end. Show your arithmetic in "
        "'reasoning' and output the final status/actual/evidence. actual = the metric's "
        "value (positive, 2 decimals; ratios as plain numbers). Status decided on the "
        f"unrounded value.\n\nCLAUSE:\n{quote}\n\n"
        f"PRECOMPUTED CATEGORY SUMS (deterministic, fills applied, decoys excluded - "
        f"build the metric from THESE, do not re-add rows yourself):\n"
        f"{json.dumps(_category_sums(rows, facts_sf), ensure_ascii=False)}\n\n"
        f"ADJUSTMENT FACTS:\n{json.dumps({k: v for k, v in facts_sf.items() if k != 'categories'}, ensure_ascii=False)}\n\n"
        "LEDGER ROWS (with categories):\n" + "\n".join(eff))
```

В `fallback_solve_voted` сменить дефолт: `samples=3` → `samples=5`.

- [ ] **Step 4: Тесты зелёные**

Run: `.venv/bin/python tests/test_category_sums.py && .venv/bin/python tests/test_sovereignty.py | tail -1 && .venv/bin/python tests/test_compute.py | tail -1`
Expected: `1/1 passed`, `4/4 passed`, `19/19 passed`

- [ ] **Step 5: Commit**

```bash
git add tests/test_category_sums.py src/judge.py
git commit -m "Solver hardening: 5 votes + precomputed category sums (no freehand arithmetic)"
```

---

### Task 4: Валидационный прогон (тёплый) + дифф

**Files:**
- Read-only: `run_log_stab.txt` (новый лог), `submission.json`, снапшот холодного `artifacts/submission_v9_cold.json` (создать до прогона)

- [ ] **Step 1: Снапшот холодного сабмишена (эталон сравнения)**

```bash
cp submission.json artifacts/submission_v9_cold.json
```

- [ ] **Step 2: Прогон (кэш свежий после холодной репетиции; живьём — решатель и пересчёты)**

```bash
source .env && export DEEPSEEK_API_KEY GEMINI_API_KEY OPENAI_API_KEY && \
export LLM_PROVIDER=deepseek && \
bash scripts/blind_run.sh 2>&1 | tee run_log_stab.txt | tail -5
```

Expected: `BLIND RUN COMPLETE`, время ≤ 30 мин, доплата ≤ $1.

- [ ] **Step 3: Дифф против холодного**

```bash
.venv/bin/python - <<'EOF'
import json
old = json.load(open("artifacts/submission_v9_cold.json"))
new = json.load(open("submission.json"))
n = 0
for sid in sorted(old["answers"]):
    for cl in sorted(old["answers"][sid]):
        a, b = old["answers"][sid][cl], new["answers"][sid][cl]
        if a == b: n += 1; continue
        d = [f"{k}: {a[k]} -> {b[k]}" for k in ("status","actual","evidence_txn_id") if a[k] != b[k]]
        print(f"{sid}/{cl}: " + "; ".join(d))
print(f"unchanged: {n}/36")
EOF
grep -E "judge_flip_reverted|denominator_narrowed" run_log_stab.txt | head
```

Expected: P9/6.1 → BREACH 0.22; P2/6.1 actual → 1.18; P7/6.1 → BREACH ≈0.36
(через `denominator_narrowed_to_opex`); НОЛЬ прочих регрессий. Если появились
новые регрессии — СТОП, разбор до любого замера.

- [ ] **Step 4: Commit лога валидации не требуется** (артефакты в .gitignore) — зафиксировать наблюдение в EXPERIMENTS.md в Task 5.

---

### Task 5: Один объявленный замер GT + документация + RUNBOOK

**Files:**
- Modify: `EXPERIMENTS.md`, `RUNBOOK.md`

- [ ] **Step 1: Замер (объявить пользователю в чате: «обращение к GT, замер #10»)**

```bash
.venv/bin/python scripts/score.py submission.json | tail -3
```

Expected (критерий заморозки из спеки): TOTAL ≥ 33.0 и ноль новых регрессий
против холодного. Если < 33 — доложить пользователю, решение об откате за ним.

- [ ] **Step 2: EXPERIMENTS.md — секция B9**

Дописать в конец файла:

```markdown
## B9 (замер #10, ОБЪЯВЛЕН): суверенитет движка + ремонт знаменателя

Диагноз холодной репетиции (32.4): вся дисперсия — статус-перевороты
ПОСЧИТАННЫХ ячеек судьёй/решателем (6/6 неверны за 9 замеров) + пустая ячейка
P7-класса (широкий opex -> отрицательный знаменатель -> решатель наугад).

Правки: (1) замок статусов - движковый ответ неприкосновенен, судья может
только добавить evidence или заполнить пустую ячейку; (2) ремонт X/(A-B) с
отрицательным знаменателем сужением B до OPEX-only (только для иначе-пустых
ячеек); (3) решатель 5 сэмплов + готовые суммы по категориям.

Замер: <TOTAL>/36. <примечания по ячейкам>
```

(`<TOTAL>` и примечания заполнить фактическими числами из Step 1.)

- [ ] **Step 3: RUNBOOK.md — тайминги и план на окно**

В таблице таймингов заменить строку про полный холодный прогон:

```markdown
Полный холодный прогон: **1 ч 47 мин, ~$2.65** (замер репетиции 8-го с обрывом
сети; без обрыва ожидается ~1ч15м-1ч30м). Тёплый перезапуск из кэша: секунды.
ДВА полных прогона в 3-часовое окно НЕ влезают - план Б только через
перезапуск из кэша (он подхватывает всё сделанное мгновенно).
```

В T-минус чеклист добавить строку:

```markdown
# баланс DeepSeek >= $5 (боевой прогон ~$2.65 + аварийный запас)
```

- [ ] **Step 4: Финальные проверки и commit**

```bash
.venv/bin/python tests/test_compute.py | tail -1
.venv/bin/python tests/test_sovereignty.py | tail -1
.venv/bin/python tests/test_denominator_repair.py | tail -1
.venv/bin/python tests/test_category_sums.py | tail -1
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import ledger,ingest,ocr_scans,classify,scenario_meta,extract,categorize,composition,run_all,judge,assemble; print('all stages import OK')"
git add EXPERIMENTS.md RUNBOOK.md
git commit -m "B9: engine sovereignty + denominator repair, measured; runbook timings updated"
```

Expected: все тесты зелёные, `all stages import OK`, коммит создан.
