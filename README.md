source .venv/bin/activate



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
```
