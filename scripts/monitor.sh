#!/bin/bash
# Живой монитор боевого прогона. Запуск:  bash scripts/monitor.sh
# Выход: Ctrl+C
cd "$(dirname "$0")/.."
LOG="${1:-run_log_BATTLE.txt}"
SPEND_BASE=""

STAGES="S0-ledger S1-ingest S1b-ocr S2-classify S2b-meta S3-extract S4-categorize S4b-composition S5-compute S6-judge S7-assemble"

while true; do
  clear
  echo "┌──────────────────────────────────────────────────────────────┐"
  echo "│              HALYK AI CHALLENGE — БОЕВОЙ ПРОГОН              │"
  echo "└──────────────────────────────────────────────────────────────┘"
  echo

  # процесс
  PID=$(pgrep -f blind_run.sh | head -1)
  if [ -n "$PID" ]; then
    ELAPSED=$(ps -o etime= -p "$PID" | tr -d ' ')
    echo "  ⏱  прогон ЖИВ, работает: $ELAPSED"
  elif grep -q "BLIND RUN COMPLETE" "$LOG" 2>/dev/null; then
    echo "  ✅ ПРОГОН ЗАВЕРШЁН — submission.json готов"
  else
    echo "  ⚠️  процесс не найден и прогон НЕ завершён — проверь лог!"
  fi
  echo

  # стадии: пройденные и текущая
  CUR=$(grep -o '== S[0-9b]* [a-z ]*==' "$LOG" 2>/dev/null | tail -1)
  echo "  Стадии (текущая: ${CUR:-старт}):"
  for s in $STAGES; do
    tag="== ${s%%-*} ${s#*-} =="
    short="${s%%-*}"
    if grep -q "== ${short} " "$LOG" 2>/dev/null; then
      if [ "$(grep -o "== S[0-9b]*" "$LOG" | tail -1)" = "== ${short}" ]; then
        echo "    ▶ $s   ← сейчас"
      else
        echo "    ✓ $s"
      fi
    else
      echo "    · $s"
    fi
  done
  echo

  # кэш и спенд
  N_CACHE=$(ls artifacts/llm_cache 2>/dev/null | wc -l | tr -d ' ')
  LAST_CACHE=$(ls -lt artifacts/llm_cache 2>/dev/null | head -2 | tail -1 | awk '{print $6, $7, $8}')
  SPEND=$(python3 -c "import json;print(round(json.load(open('artifacts/llm_spend.json'))['total_usd'],2))" 2>/dev/null)
  echo "  💾 LLM-вызовов в кэше: $N_CACHE (последний: ${LAST_CACHE:-—})"
  echo "  💰 спенд проекта: \$${SPEND:-?}"
  echo

  # здоровье: ошибки соединения, ретраи, флаги
  N_ERR=$(grep -c "Connection\|nodename\|Traceback\|failed" "$LOG" 2>/dev/null)
  echo "  🩺 ошибок/ретраев в логе: $N_ERR"
  echo
  echo "  ── последние строки лога ──────────────────────────────────"
  tail -6 "$LOG" 2>/dev/null | sed 's/^/  /' | cut -c1-100
  echo
  echo "  (обновление каждые 20 сек; Ctrl+C — выход)"
  sleep 20
done
