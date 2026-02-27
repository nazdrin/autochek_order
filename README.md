
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


