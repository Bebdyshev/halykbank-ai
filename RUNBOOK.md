# Ранбук дня сдачи (окно 3 часа)

Цель: новый датасет → `submission.json` одной командой, с запасом времени.
Тайминги холодного прогона замерены на репетиции — см. таблицу ниже.

## T-минус (накануне, 8-е): проверить готовность

```bash
cd ~/Documents/Github/halykbank-ai
git status                 # чистое дерево, ветка main
cat .env                   # DEEPSEEK_API_KEY и GEMINI_API_KEY живы, LLM_PROVIDER=deepseek
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import llm; print(llm.STRONG)"
.venv/bin/python tests/test_compute.py          # 19/19
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import ledger,ingest,ocr_scans,classify,scenario_meta,extract,categorize,composition,run_all,judge,assemble; print('all stages import OK')"  # ловит NameError, который ast не видит
# баланс DeepSeek: https://platform.deepseek.com  (нужно >= $5 запаса)
# квоты Gemini обновляются в полночь по Тихоокеанскому — OCR-фолбэк должен быть жив
which tesseract            # OCR-фолбэк последней надежды (rus+kaz+eng установлены)
# ПРЕФЛАЙТ VISION: один пробный vision-вызов (страница любого PDF) — убедиться,
# что хотя бы один из провайдеров жив ДО старта окна; баланс OpenAI тоже проверить
```

## День Х: порядок действий

### 1. Положить новый датасет (5 мин)

Ожидаемая структура — та же, что у практики:

```
case-related-docs/
├── master_ledger_*.csv        # реестр транзакций (имя может отличаться!)
├── submission_template.json   # шаблон с ячейками
└── documents/                 # все PDF
```

Практический набор предварительно убрать: `mv case-related-docs case-related-docs-practice`.

Имя CSV-леджера и префикс транзакций определяются из данных автоматически
(glob по `*.csv` с приоритетом имени, содержащего «ledger»; префикс — из
первой строки). Жёстких имён в коде больше нет.

### 2. Очистить производные артефакты ОТ ПРАКТИКИ (1 мин)

```bash
mv artifacts/llm_cache artifacts/llm_cache_practice   # кэш практики НЕ удалять
mkdir -p artifacts/llm_cache
rm -f artifacts/*.json
rm -rf artifacts/parsed_docs artifacts/ocr artifacts/scan_pages
```

Кэш обязательно отложить, не оставить: старые записи безвредны (другие тексты →
другие ключи), но чистый кэш даёт честные тайминги и изолирует прогоны.

### 3. Запуск (одна команда)

```bash
source .env && export DEEPSEEK_API_KEY GEMINI_API_KEY OPENAI_API_KEY
export LLM_PROVIDER=deepseek
time bash scripts/blind_run.sh 2>&1 | tee run_log.txt
```

Мониторинг из второго терминала:

```bash
watch -n 30 'python3 -c "import json; d=json.load(open(\"artifacts/llm_spend.json\")); print(d[\"total_usd\"])"; ls -lt artifacts/llm_cache | head -2'
```

### 4. Проверки перед сдачей (5 мин)

```bash
.venv/bin/python -c "
import json
s = json.load(open('submission.json'))
t = json.load(open('case-related-docs/submission_template.json'))
assert set(s['answers']) == set(t['answers']), 'scenario keys mismatch'
for sid in t['answers']:
    assert set(s['answers'][sid]) == set(t['answers'][sid]), sid
    for cl, c in s['answers'][sid].items():
        assert c['status'] in ('COMPLIANT','BREACH'), (sid, cl)
        assert isinstance(c['actual'], (int,float)) and c['actual'] > 0, (sid, cl)
print('SUBMISSION VALID:', sum(len(v) for v in s['answers'].values()), 'cells')
print('team:', s['team'], '| email:', s['contact_email'], '| model:', s['model'])"
```

`assemble.py` уже проверяет всё это в конвейере, но перед отправкой — руками ещё раз.
Поля team/contact_email/model заданы в `src/assemble.py` (проверить актуальность!).

### 5. Сдача → отправить `submission.json`.

## Замеренные тайминги (репетиция 8-го, 8 воркеров, DeepSeek)

Полный холодный прогон: **44 мин 20 сек, $2.38** (8 воркеров, DeepSeek v4,
OCR через Gemini-фолбэк). Тёплый перезапуск из кэша: **16 секунд, $0.00**.

| Стадия | Время (приблизительно) | Вызовов |
|---|---|---|
| ledger + ingest + OCR | ~6 мин | ~7 vision |
| classify (200 доков) | ~8 мин | ~210 flash |
| scenario_meta + extract | ~8 мин | ~60 pro |
| categorize (2 прохода + тай-брейки) | ~15 мин | ~40 |
| composition | ~4 мин | ~14 pro |
| judge + сборка | ~4 мин | ~15 pro |

Запас против 3-часового окна — четырёхкратный: помещаются два полных
перезапуска с нуля плюс сборка.

## Гарантии организаторов (подтверждено 8-го) → как читать флаги

Организаторы подтвердили: **договор банковского займа и KYC-досье ЕСТЬ ВСЕГДА** для
каждого заёмщика; **устаревших редакций KYC не бывает** (одно действующее издание).

Отсюда — важное правило чтения флагов на скрытом наборе:

- Если sanity-check (`classify.py`, минута ~8) напишет `no KYC dossier` или
  `covenants_missing` / `0 active agreements` — это **НЕ пропущенный документ**
  (их гарантированно положили), а **провал КЛАССИФИКАЦИИ**: мы неверно определили
  тип реального документа. Реакция: не принимать пробел. Найти документ заёмщика
  глазами в `case-related-docs/documents/`, проверить `artifacts/doc_index.json`,
  при нужде перезапустить `classify.py` (сильная модель уже включается на шатких
  ответах) или временно поднять долю strong-повторов.
- KYC берётся НЕЗАВИСИМО от authority (`extract.py`: `dt == "kyc_dossier"` без
  фильтра) — мисметка издания KYC документ не выбросит, чинить не нужно.
- Флаг `duplicate ... facts for <sid>` = два документа классифицированы как KYC/
  договор одного заёмщика (реальный + приманка-двойник). Свериться с doc_index,
  оставить настоящий.

## План Б (если что-то горит)

- **Стадия зависла** (нет записей в кэш > 10 мин): `pkill -f <stage>.py`, перезапустить
  `blind_run.sh` — всё сделанное вернётся из кэша мгновенно, продолжит с места обрыва.
- **DeepSeek лёг**: `export LLM_PROVIDER=openai` (если есть баланс) — кэш по-провайдерный,
  прогресс НЕ переносится; либо ждать (Gemini-фолбэк подхватывает единичные ошибки сам).
- **Время кончается, судья не успевает**: сабмишен можно собрать без судьи из сырых
  ответов движка: `.venv/bin/python src/assemble.py artifacts/answers.json submission.json`
  (это ~90% качества полного конвейера).
- **Совсем всё горит**: минимальный конвейер = ledger → ingest → ocr → classify →
  scenario_meta → extract → categorize → run_all → assemble (пропустить composition
  и judge): убрать их строки из blind_run.sh.

## Чего НЕ делать в день Х

- Не менять промпты/схемы «на удачу» — любое изменение инвалидирует кэш соответствующей
  стадии и ломает план Б перезапуска.
- Не запускать score.py — на новом датасете нет ground_truth, а на практике это
  бессмысленно тратит время.
- Не коммитить в процессе — только после сдачи.
