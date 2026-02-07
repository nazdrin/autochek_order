RPA Biotus — оформление заказов

Автоматизация оформления заказов на opt.biotus.ua через Playwright + Chrome CDP.
Проект рассчитан на локальную работу (браузер запускается вручную, скрипты подключаются по CDP).

⸻
BIOTUS_BASE_URL=https://opt.biotus.ua
BIOTUS_LOGIN=ibezdrabko@icloud.com
BIOTUS_PASSWORD=397Aa397$
Общая логика

python -u scripts/orchestrator.py --once
rm -f .orch_state.json
python -u scripts/orchestrator.py
rm -f .orch_state.json
python -u scripts/orchestrator.py --once
Требования
	•	macOS
	•	Python 3.11
	•	Google Chrome (desktop)
	•	Аккаунт opt.biotus.ua

⸻

Установка окружения

cd rpa_biotus

python3.11 -m venv .venv
source .venv/bin/activate

python -m pip install -U pip
pip install playwright python-dotenv
python -m playwright install chromium

Проверка:

python -c "import playwright, dotenv; print('ok')"


⸻

Переменные окружения

Создай файл .env на основе .env.example:

BIOTUS_USE_CDP=1
BIOTUS_CDP_ENDPOINT=http://127.0.0.1:9222

BIOTUS_BASE_URL=https://opt.biotus.ua
BIOTUS_LOGIN=your_email
BIOTUS_PASSWORD=your_password

BIOTUS_TEST_SKU=MNW-532832
BIOTUS_AFTER_LOGIN_URL=https://opt.biotus.ua/sales/order/history?uid=...

BIOTUS_CITY_QUERY=Бердичів
BIOTUS_CITY_MUST_CONTAIN=Житомирська

BIOTUS_TIMEOUT_MS=15000

⚠️ .env никогда не коммитится.

⸻

Запуск Chrome (обязательно)

Перед запуском скриптов нужно вручную запустить Chrome:

/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-rpa-profile"

BIOTUS_USE_CDP=1 python tools/save_state.py

	•	В этом окне можно залогиниться вручную
	•	Сессия сохраняется между запусками

⸻

Запуск скриптов

По шагам (рекомендуется при отладке)

BIOTUS_USE_CDP=1 python scripts/step2_search.py
BIOTUS_USE_CDP=1 python scripts/step3_add_to_cart.py
BIOTUS_USE_CDP=1 python scripts/step4_checkout.py
BIOTUS_USE_CDP=1 python scripts/step5_select_drop_tab.py
BIOTUS_USE_CDP=1 python scripts/step5_fill_name_phone.py
BIOTUS_USE_CDP=1 python scripts/step5_select_city.py

Каскадно

set -e
BIOTUS_USE_CDP=1 python scripts/step2_search.py && \
BIOTUS_USE_CDP=1 python scripts/step3_add_to_cart.py && \
BIOTUS_USE_CDP=1 python scripts/step4_checkout.py && \
BIOTUS_USE_CDP=1 python scripts/step5_select_drop_tab.py && \
BIOTUS_USE_CDP=1 python scripts/step5_fill_name_phone.py && \
BIOTUS_USE_CDP=1 python scripts/step5_select_city.py && \
BIOTUS_USE_CDP=1 python scripts/step6_select_np_branch.py && \
BIOTUS_USE_CDP=1 python scripts/step7_fill_ttn.py && \
BIOTUS_USE_CDP=1 python scripts/step8_attach_invoice_file.py

set -e
BIOTUS_USE_CDP=1 python scripts/step2_3_add_items_to_cart.py && \
BIOTUS_USE_CDP=1 python scripts/step4_checkout.py && \
BIOTUS_USE_CDP=1 python scripts/step5_select_drop_tab.py && \
BIOTUS_USE_CDP=1 python scripts/step5_fill_name_phone.py && \
BIOTUS_USE_CDP=1 python scripts/step5_select_city.py && \
BIOTUS_USE_CDP=1 python scripts/step6_1_select_np_terminal.py && \
BIOTUS_USE_CDP=1 python scripts/step7_fill_ttn.py && \
BIOTUS_USE_CDP=1 python scripts/step8_attach_invoice_file.py && \
BIOTUS_USE_CDP=1 python -u scripts/step9_confirm_order.py





BIOTUS_BASE_URL=https://opt.biotus.ua
BIOTUS_LOGIN=ibezdrabko@icloud.com
BIOTUS_PASSWORD=397Aa397$

chmod +x tools/anti_focus.sh

./tools/anti_focus.sh



Автор: Дмитрий

запуск

/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-rpa-profile"


  source .venv/bin/activate
BIOTUS_USE_CDP=1 python tools/save_state.py


BIOTUS_HEADLESS=1 BIOTUS_STATE_FILE=.biotus_state.json python -u scripts/orchestrator.py

BIOTUS_USE_CDP=0 BIOTUS_HEADLESS=1 BIOTUS_STATE_FILE=.biotus_state.json python -u scripts/orchestrator.py
