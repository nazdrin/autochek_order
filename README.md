source .venv/bin/activate
git add .
git commit -m "фикс протеина монстра и добавок"

git push origin main

 План с приоритетами сохранён в память: /Users/dmitrijnazdrin/.codex/memories/supplier4_monsterlab_search_stability_plan_2026-03-23.md.
 Текущее состояние и внесённые стабилизационные правки по Monsterlab: `/Users/dmitrijnazdrin/Projects/rpa_biotus/docs/supplier4_monsterlab_status_2026-04-06.md`.
Перед запуском скриптов нужно вручную запустить Chrome:

/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-rpa-profile"


BIOTUS_BASE_URL=https://opt.biotus.ua
BIOTUS_LOGIN=ibezdrabko@icloud.com
BIOTUS_PASSWORD=397Aa397$
Запуск скрипта

python -u scripts/orchestrator.py --once
rm -f .orch_state.json
python -u scripts/orchestrator.py
rm -f .orch_state.json
 

Установка окружения

cd rpa_biotus

python3.11 -m venv .venv
source .venv/bin/activate

python -m pip install -U pip
pip install playwright python-dotenv
python -m playwright install chromium


⸻

Supplier6 (proteinplus.pro): очистка корзины

ENV:
- `SUP6_BASE_URL=https://proteinplus.pro`
- `SUP6_HEADLESS=0`
- `SUP6_TIMEOUT_MS=20000`
- `SUP6_STORAGE_STATE_FILE=.state_supplier6.json`
- `SUP6_ITEMS=SKU1:2,SKU2:1` (для шага наполнения)
- `SUP6_CLEAR_CART_PAUSE_SECONDS=20` (пауза после `--clear-cart`)

Запуск только шага очистки корзины:

```bash
python -u scripts/supplier6_run_order.py --clear-cart
```

Запуск только шага 3 (наполнение корзины):

```bash
SUP6_ITEMS="ART1:2,ART2:1" python -u scripts/supplier6_run_order.py --step=3
```

Полный flow supplier6 (login -> clear_cart -> step3 add_items):

```bash
SUP6_ITEMS="ART1:2,ART2:1" SUP6_STAGE=run python -u scripts/supplier6_run_order.py

Если хочешь делать по шагам:

  1. Убить все процессы Chrome:

  pkill -f "Google Chrome"

  2. Удалить профиль CDP:

  rm -rf /tmp/chrome-cdp

  3. Сбросить состояние оркестратора:

  rm -f .orch_state.json

  4. Поднять Chrome с debug-портом:

  /Applications/Google\ Chrome.app/Contents/MacOS/Google\Chrome \--remote-debugging-port=9222 \

  5. В другом терминале запустить оркестратор:

  source .venv/bin/activate
  Если хочешь без шумных логов Chrome:

  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-cdp \
    --window-size=1600,1000 \
    >/tmp/biotus-chrome.log 2>&1 &
```
