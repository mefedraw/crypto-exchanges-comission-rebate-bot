Гайд для Claude Code по этому репозиторию. Полное ТЗ — в TZ_commission_bot.md (читай его перед началом работы).
Что это за проект
Telegram-бот для одного владельца. Считает реферальную (affiliate) комиссию по uid и диапазону дат на крипто-биржах для расчёта выплат. Поток: выбор биржи → ввод uid → ввод периода (с — по) → сумма комиссии с разбивкой по валютам.
Биржи: Bitget, Bybit, WEEX, Gate, OKX, KuCoin, MEXC (7 штук). Toobit и BitMart НЕ поддерживаем (нет affiliate-API).
Стек

Python 3.11+, aiogram 3.x, httpx, pydantic v2, cryptography (Fernet)
Зависимости через poetry/uv, версии пинуются
Деплой: Docker + docker-compose, long polling (без входящих портов)
FSM state: memory (одному пользователю хватит)

Команды
bash# установка
uv sync                      # или poetry install

# запуск локально
python -m bot.main

# тесты / линт / типы
pytest
ruff check . && ruff format --check .
mypy bot

# docker
docker compose up -d --build
docker compose logs -f

# подготовить зашифрованный секрет (Fernet)
python -m bot.security.secrets encrypt   # утилита для разовой шифровки ключа

Перед коммитом всегда: ruff check, mypy bot, pytest.

Архитектура
Adapter-паттерн. Бот НЕ знает про специфику бирж — только через интерфейс ExchangeAdapter.
bot/
  main.py            # точка входа, polling
  config.py          # pydantic Settings, строит registry доступных бирж
  security/          # access (whitelist), secrets (Fernet), ratelimit
  handlers/          # start, flow (FSM), errors
  keyboards.py
  exchanges/
    base.py          # ExchangeAdapter (ABC) + CommissionResult/CommissionLine
    signing.py       # хелперы HMAC-подписи
    <exchange>.py    # по одному адаптеру на биржу
    registry.py      # регистрирует только биржи, для которых заданы все ключи
  utils/
    dates.py         # парсинг дат + нарезка периода на окна биржи
    money.py         # суммирование Decimal, форматирование
tests/
Каждый адаптер реализует async get_commission(uid, date_from, date_to) -> CommissionResult:
нарезает период по max_window_days, листает пагинацию, суммирует по валютам, заполняет notes/settlement_note.
Правила, которые НЕЛЬЗЯ нарушать
Безопасность

Доступ только по whitelist из двух ролей: USER_TELEGRAM_IDS (список владельцев, пользуются ботом) и DEVELOPER_TELEGRAM_ID (получает алерты о сбоях, тоже может пользоваться). Whitelist = все user-id + developer. Чужие апдейты — молча игнорировать (не отвечать), факт логировать. Алерты разработчику (bot/alerts.py): необработанные ошибки и проблемы с ключами API.
Секреты только из окружения. Никаких ключей/токенов в коде или гите. .gitignore обязан содержать .env, *.key, secrets/.
API-ключи бирж — минимальные права (affiliate/read). Никогда не предполагать и не требовать права на трейд/withdraw. Bybit — только разрешение Affiliate.
Не логировать секреты. Маскировать ключи (****), не дампить заголовки запросов и сырые тела с токенами.
TLS не отключать (httpx verify=True). Все запросы по HTTPS, с таймаутами и бэкоффом (ретрай только на сетевые/5xx, не на 4xx).
Docker: non-root, cap_drop: [ALL], read-only FS где возможно.

Деньги

Только Decimal для сумм. Никакого float.
Суммировать по валютам отдельно; не складывать разные активы.

Честность данных

API отдаёт начисленную (accrued), а не гарантированно «к выплате» комиссию. В каждом ответе бота — дисклеймер.
Bybit: произвольный диапазон дат недоступен (только скользящие 30/365 дней) — выводить предупреждение, не выдавать за точную сумму периода. Аналогично проверить OKX.

Особенности бирж (кратко; детали и ссылки — в ТЗ)
БиржаЭндпоинтФильтр uidПроизвольный диапазонНадёжность спекиGate/api/v4/rebate/partner/commission_historyuser_idда (окно ≤30 дн)высокаяKuCoin/api/v2/affiliate/queryMyCommissionuserIdда (rebateStartAt/EndAt)высокаяMEXC/api/v3/rebate/affiliate/commission🔎да (startTime/endTime)средне-высокаяBitget/api/v2/broker/customer-commissionsuidда (окно ≤30 дн)средняяOKX/api/v5/affiliate/invitee/detailuid🔎 возможно только накопл.средняяBybit/v5/user/aff-customer-infouidНЕТ (30/365 скольз.)средняяWEEX/rebate/affiliate/getAffiliateCommission🔎🔎низкая — проверять всё
🔎 = подтвердить по живой документации перед реализацией (часть доков рендерится через JS, поля могли быть не видны при ресёрче).
Порядок реализации
Каркас → security-ядро → base/signing/utils → FSM+UI (на заглушке) → адаптеры в порядке надёжности (Gate, KuCoin → MEXC, Bitget, OKX, Bybit → WEEX последним) → тесты → README.
Тесты
Юнит: подпись каждой биржи (сверять с примером из доки), нарезка дат (граничные 1/30/31/60), суммирование Decimal, валидация ввода, whitelist middleware. Адаптеры — на httpx-mock с фикстурами из доков, живой API в CI не дёргать.
Стиль кода

Async везде, где I/O. Типизация обязательна (mypy strict по возможности).
Пользовательский ввод (uid, даты) — строго валидировать перед подстановкой в URL/подпись.
Комментарии в коде помечать # VERIFIED: (подтверждено по живой доке) или # ASSUMED: (предположение, требует проверки) для каждого спорного места по биржам.