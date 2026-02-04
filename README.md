RPA Biotus — оформление заказов

Автоматизация оформления заказов на opt.biotus.ua через Playwright + Chrome CDP.
Проект рассчитан на локальную работу (браузер запускается вручную, скрипты подключаются по CDP).

⸻

Общая логика

Скрипты выполняются по шагам, каждый шаг — отдельный файл:
	1.	step1_login.py — логин (опционально, если используем сохранённую сессию)
	2.	step2_search.py — поиск товара по SKU
	3.	step3_add_to_cart.py — добавление товара в корзину
	4.	step4_checkout.py — переход к оформлению заказа
	5.	step5_select_drop_tab.py — выбор режима «Для відправки дроп»
	6.	step5_fill_name_phone.py — заполнение имени и телефона
	7.	step5_select_city.py — выбор города (SlimSelect)
	8.	step6_select_np_branch.py — выбор отделения Новой Почты (в разработке)

Каждый шаг можно запускать отдельно или каскадно.

⸻

Структура проекта

rpa_biotus/
├── scripts/
│   ├── step1_login.py
│   ├── step2_search.py
│   ├── step3_add_to_cart.py
│   ├── step4_checkout.py
│   ├── step5_select_drop_tab.py
│   ├── step5_fill_name_phone.py
│   ├── step5_select_city.py
│   └── step6_select_np_branch.py
├── artifacts/            # скриншоты и отладочные артефакты
│   └── .gitkeep
├── .env                  # локальные секреты (НЕ коммитится)
├── .env.example          # пример env
├── .gitignore
├── requirements.txt
└── README.md


⸻

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
BIOTUS_USE_CDP=1 python scripts/step5_select_city.py
BIOTUS_USE_CDP=1 python -u scripts/step6_select_np_branch.py
BIOTUS_USE_CDP=1 python -u scripts/step7_fill_ttn.py
BIOTUS_USE_CDP=1 python -u scripts/step8_attach_invoice_file.py


⸻

Текущее состояние стабильности

Шаг	Статус
Поиск товара	✅ стабильно
Добавление в корзину	✅ стабильно
Оформить заказ	⚠️ нестабильно (мигание)
Имя + телефон	⚠️ нестабильно
Выбор города	⚠️ частично стабильно
Отделение НП	❌ не реализовано


⸻

Отладка
	•	Все скриншоты сохраняются в artifacts/
	•	При ошибках смотри последний .png
	•	Для SlimSelect выбор города всегда через клик по опции

⸻

Принципы работы над проектом
	•	Сначала анализ DOM и состояний
	•	Потом минимальные правки
	•	Один шаг — один файл
	•	Никакой магии с Enter для SlimSelect

⸻

Дальнейшие шаги
	•	Стабилизация step4_checkout.py
	•	Полная переработка step5_fill_name_phone.py
	•	Реализация step6_select_np_branch.py
	•	Выделение общего helper-модуля

⸻

Автор: Дмитрий