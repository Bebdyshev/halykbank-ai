#!/bin/bash
# Blind end-to-end pipeline run: no ground-truth access anywhere in the chain.
# scripts/score.py (which reads ground_truth.json) is NOT invoked here - run it
# separately, once, as the final measurement.
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python

echo "== wiping derived artifacts (keeping llm_cache + parsed_docs/ocr) =="
rm -f artifacts/{doc_index,scenario_meta,facts,categorized_ledger,answers,answers_judged,audit_trails,flags,judge_report}.json

echo "== S0 ledger =="        && $PY src/ledger.py
echo "== S1 ingest =="        && $PY src/ingest.py | tail -2
echo "== S1b ocr =="          && $PY src/ocr_scans.py | tail -2
echo "== S2 classify =="      && $PY src/classify.py | tail -8
echo "== S2b scenario meta ==" && $PY src/scenario_meta.py | tail -3
echo "== S3 extract =="       && $PY src/extract.py | tail -5
echo "== S4 categorize =="    && $PY src/categorize.py | tail -13
echo "== S5 compute =="       && $PY src/run_all.py | tail -20
echo "== S6 judge =="         && $PY src/judge.py | tail -25
echo "== S7 assemble =="      && $PY src/assemble.py artifacts/answers_judged.json submission.json

$PY -c "import sys; sys.path.insert(0,'src'); from llm import total_spend; print(f'LLM spend this project: \${total_spend():.2f}')"
echo "BLIND RUN COMPLETE -> submission.json (ground truth NOT consulted)"
