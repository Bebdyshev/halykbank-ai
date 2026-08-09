# Halyk AI Challenge — ковенант-пайплайн

Агентный пайплайн для проверки финансовых ковенантов: читает ~300 «грязных» PDF
(договоры займа, KYC-досье, аудиторские отчёты пяти фирм, казначейские мемо,
приманки) + реестр транзакций с ~35% мусорных строк, и для каждого заёмщика ×
каждого пункта (6.1/6.2/…) выдаёт `COMPLIANT`/`BREACH`, фактическое значение
метрики и транзакцию-доказательство → `submission.json`.

**Принцип архитектуры: LLM читает и судит — детерминированный код считает и
собирает.** Модель никогда не делает арифметику; все суммы, курсы, периоды и
пороги применяет движок на Python.

## Требования

- Python 3.12
- poppler (`pdftotext`/`pdfimages`/`pdftoppm`) — парсинг PDF
- tesseract + языки rus/kaz/eng — аварийный OCR-фолбэк
- API-ключи: DeepSeek (основной), Gemini (бесплатный фолбэк), OpenAI (vision/резерв)

```bash
# macOS
brew install poppler tesseract tesseract-lang
# Linux
sudo apt install poppler-utils tesseract-ocr

python3.12 -m venv .venv
.venv/bin/python -m pip install openai google-genai
```

Создать `.env` в корне (в git не попадает):

```
DEEPSEEK_API_KEY=sk-...
GEMINI_API_KEY=...
OPENAI_API_KEY=sk-...
LLM_PROVIDER=deepseek
```

## Данные

Положить датасет так, чтобы в корне репо была папка:

```
case-related-docs/
├── master_ledger_*.csv        # реестр транзакций (имя ищется по глобу)
├── submission_template.json   # шаблон ячеек (сценарии × пункты)
└── documents/                 # все PDF
```

Имена CSV, префиксы транзакций и состав пунктов определяются из данных
автоматически (включая нестандартные форматы вида `TXN-KC-CAP-29` и не-`ACC-`
счета).

## Запуск

```bash
source .env && export DEEPSEEK_API_KEY GEMINI_API_KEY OPENAI_API_KEY
export LLM_PROVIDER=deepseek
time bash scripts/blind_run.sh 2>&1 | tee run_log.txt
```

Холодный прогон на ~300 документах / 27 сценариях: **~1.5–2 часа, ~$4–5**
(DeepSeek, 8 воркеров). Тёплый перезапуск из кэша: секунды. При обрыве
сети/падении — просто перезапустить ту же команду: content-addressed кэш
(`artifacts/llm_cache/`) мгновенно поднимает всё досчитанное.

Живой монитор из второго терминала:

```bash
bash scripts/monitor.sh run_log.txt
```

Результат: `submission.json` + артефакты каждой стадии в `artifacts/`
(doc_index, facts, categorized_ledger, answers, audit_trails, flags…).

## Стадии конвейера (scripts/blind_run.sh)

| Стадия | Что делает |
|---|---|
| S0 `ledger.py` | парсинг CSV, сценарий = сегмент txn-id, отсев приманок |
| S1 `ingest.py` | pdftotext -layout всех PDF, детект сканированных страниц |
| S1b `ocr_scans.py` | vision-транскрипция сканов (gpt-5 → Gemini → tesseract) |
| S2 `classify.py` | LLM-классификация типов/authority/владельца + sanity-check |
| S2b `scenario_meta.py` | имя заёмщика, отрасль, период ковенанта из договора |
| S3 `extract.py` | ковенанты→DSL (2 прохода + арбитр + структурные стражи), KYC, аудит-факты, групповые отчёты |
| S4 `categorize.py` | категория каждой строки (2 прохода + батчевый тай-брейк) |
| S4b `composition.py` | явный состав компонентов по аудитору (gate: принимается только при сверке с заявленной цифрой) |
| S5 `run_all.py` | детерминированный движок: формулы, пороги, периоды, FX, flip-тест evidence |
| S6 `judge.py` | LLM-аудит ячеек; **суверенитет движка**: посчитанный ответ неприкосновенен, судья лишь добавляет evidence и чинит пустые ячейки |
| S7 `assemble.py` | валидация и сборка `submission.json` |

Ключевые механизмы устойчивости: стражи извлечения (пустые/NOISE/дублирующие
компоненты, «отношение обязано делить на реальный компонент»), ремонт
отрицательного знаменателя, дедуп идентичных слагаемых, замок статусов после
судьи, голосующий решатель (5 сэмплов) только для пустых ячеек, санитизация
evidence по членству в леджере.

## Инструменты финала

```bash
# валидация/чинка сабмишена (exit 0 = можно сдавать)
.venv/bin/python scripts/submission_doctor.py submission.json --fix fixed.json

# досье одной ячейки за секунду: цитата, формула, суммы, пересчёт
.venv/bin/python scripts/cell_brief.py H3 6.2
```

## Тесты и воспроизведение

```bash
.venv/bin/python tests/test_compute.py            # движок: 19 тестов
.venv/bin/python tests/test_sovereignty.py        # замок статусов
.venv/bin/python tests/test_denominator_repair.py # ремонт знаменателя
.venv/bin/python tests/test_category_sums.py      # суммы для решателя
```

Замеренные результаты (журнал экспериментов — `EXPERIMENTS.md`, все обращения
к ground truth объявлены):

- практический набор (12 сценариев × 3 пункта): **33.5/36 (93.1%)** слепым
  прогоном, стабилизировано против ресэмплинга;
- скрытый набор (27 сценариев, 84 ячейки): полный прогон без единого краша +
  ручная документальная хирургия 11 ячеек-ловушек (см. коммит `a9c94d5`).

Из-за температурного сэмплинга LLM повторный холодный прогон может отличаться
в единичных ячейках; детерминированная часть (движок, стражи, суверенитет)
воспроизводится точно. Протокол дня сдачи — `RUNBOOK.md`.

## Структура репо

```
src/            конвейер (llm.py — мультипровайдерная обёртка с кэшем)
scripts/        blind_run.sh, monitor.sh, доктор, бриф, score.py (реплика скорера)
tests/          юнит-тесты движка и стражей
artifacts/      кэш LLM + артефакты стадий (в .gitignore)
docs/           спеки и планы (superpowers)
EXPERIMENTS.md  журнал гипотез и замеров B1→B9
RUNBOOK.md      протокол дня сдачи, тайминги, план Б
PLAN.md         исходный план и таксономия ловушек
```
