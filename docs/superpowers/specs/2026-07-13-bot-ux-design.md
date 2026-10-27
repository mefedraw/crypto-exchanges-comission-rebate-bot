# Phase 4 — Bot UX (self-service payout) design

Статус: дизайн (автономный, владелец в отъезде). Реализуется subagent-TDD после плана.
Источники: `PAYOUT_BUSINESS_LOGIC.md` §1-5, `PAYOUT_SECURITY_RELIABILITY.md` (action_token, TG-нюансы),
`DB_SCHEMA.md` (users/wallets/exchange_accounts/action_tokens/audit_events). aiogram 3.x
(pinned 3.13.1): Router + StatesGroup/State + FSMContext, MemoryStorage в тестах, DI хэндлеров по kwargs.

Цель фазы: пользователь через бота управляет кошельком и UID'ами, видит статистику/баланс,
инициирует вывод. **Реальная отправка денег НЕ здесь** — это Phase 5 (payout-executor). Здесь
вывод доходит до создания `payouts`-строки в статусе `reserved` + резервирования начислений; сам
on-chain перевод — заглушка/интерфейс. TG-токен живой НЕ дёргаем; хэндлеры тестируем на моках
апдейтов + реальный Postgres (testcontainers) для БД-логики.

## Что уже есть (переиспользуем, не переписываем)
- `bot/main.py::build_dispatcher` — whitelist-middleware (outer), rate-limit, роутеры, DI через `dp[...]`.
- `bot/security/access.py` — WhitelistMiddleware (чужих молча игнорит, факт логирует).
- `bot/security/secrets.py::SecretCipher` — Fernet; переиспользуем для шифрования адреса кошелька at rest.
- `bot/db/` — модели + engine/sessionmaker (Phase 2). `bot/payout/` (ранги/комса/кулдаун/экспирация).
- `bot/accrual/repository.py` — часть чтений (баланс/ранг) переиспользуем/дополним.

## Архитектура фазы

Новый пакет `bot/botui/` (не путать с legacy `bot/handlers/flow.py` — тот про калькулятор комсы):
- `bot/botui/states.py` — FSM StatesGroup'ы (WalletFlow, UidFlow, WithdrawFlow).
- `bot/botui/keyboards.py` — инлайн-клавиатуры (главное меню, подтверждения, список UID).
- `bot/botui/wallet.py` — роутер: добавить/сменить BEP20-адрес (валидация EVM checksum, шифрование, история).
- `bot/botui/accounts.py` — роутер: добавить/список/удалить UID; обработка коллизии `(exchange, uid)` (first-come + флаг admin).
- `bot/botui/stats.py` — роутер: статистика (по UID и суммарно), эффективный ранг/fee, доступный баланс, дата след. вывода.
- `bot/botui/withdraw.py` — роутер: инициировать вывод (кулдаун + min-payout + action_token confirm → reserve).
- `bot/botui/menu.py` — /start главное меню + навигация (upsert users-строки при первом контакте).
- `bot/botui/service.py` — прикладной слой (без aiogram): чистые/async-функции над репозиторием, чтобы
  хэндлеры были тонкими и легко тестировались отдельно от Telegram.
- Дополнить `bot/botui/repository.py` (или расширить `bot/accrual/repository.py`) чтениями/записями:
  upsert_user, get/replace wallet, add/list/archive account (с проверкой коллизии), available_balance,
  effective_rank_now, last_success_withdrawal_at, reserve_accruals_for_payout, create/consume action_token,
  write audit_event.
- DI: `dp["sessionmaker"] = make_sessionmaker(engine)` в `build_dispatcher`; хэндлеры получают `session`
  через per-update middleware (открывает сессию на апдейт) ИЛИ берут sessionmaker из workflow-data и
  открывают сессию сами. **Решение: middleware `DbSessionMiddleware`** (message+callback), кладёт
  `session` в data, коммитит на успехе, ролит на исключении — единый транзакционный контур на апдейт.

### Эффективный ранг «сейчас» (для fee/кулдауна в UI)
`effective_rank_now(user)` = `max(achieved(текущий месяц по накоплению), achieved(M-1), achieved(M-2))`
(формула 4.3). achieved(M-1/M-2) — из `rank_history`; текущий месяц — `rank_for_gross(user_month_gross(текущий))`.
Переиспользуем `bot/payout/ranks.effective_rank` + `rank_for_gross`. Читаем 3 значения rank_history + MTD-gross.

## FSM-потоки (states.py)

```
WalletFlow:   waiting_address            # ждём BEP20-адрес текстом
UidFlow:      choosing_exchange -> waiting_uid
WithdrawFlow: confirming                 # показан action_token-подтверждение
```
Меню и просмотр статистики — без состояний (стейт-лесс callback'и). Отмена (`/cancel` или кнопка) в любом
состоянии → `state.clear()` → меню.

## Ключевые правила реализации (из бизнес/безопасности)

1. **Whitelist** уже глобально (outer middleware) — все botui-хэндлеры доступны только whitelisted. UID/кошелёк
   привязаны к `message.from_user.id` (= users.id = telegram id). Никогда не доверяем id из callback-data — только из апдейта.
2. **Кошелёк:** один active BEP20 на юзера (partial-unique `uq_wallet_active`). Смена = старый `replaced`,
   новый `active` + `audit_event`. Валидация: EVM checksum-адрес (web3/eth_utils нет в deps — реализуем
   лёгкую проверку EIP-55 вручную на `hashlib`/keccak? keccak нет в stdlib). **ASSUMED:** проверяем формат
   `0x` + 40 hex; полноценный EIP-55 checksum отложить (или добавить `eth-hash`/`pycryptodome` — согласовать).
   Адрес хранится: `address_encrypted` (Fernet), `address_hash` (keccak/sha256 для lookup), `address_masked`
   (0xABCD…1234) для показа. Chain фиксирован кнопкой (не вводится).
3. **UID:** пара `(exchange, uid)` глобально уникальна. Добавление занятой пары другим юзером → отказ +
   `audit_event` + (позже) флаг admin. `accrual_since` = `REBATE_EPOCH` (или позже) — ставится при добавлении;
   бэкфилл делает accrual-worker (Phase 3), не бот.
4. **Статистика:** доступный баланс = Σ net по `available` начислениям (не expired/paid/reserved), в срезе
   FIFO; показываем эффективный ранг, fee, дату след. вывода (кулдаун), ближайшую экспирацию (напоминание 💡).
5. **Вывод (WithdrawFlow):**
   - Проверки перед подтверждением: ранг payable (≥1), кулдаун `next_withdrawal_allowed(last_success, rank)`
     прошёл, доступный `net ≥ MIN_PAYOUT_USDT` (после комсы; газ вычтется в Phase 5).
   - Генерим **action_token** (одноразовый, TTL, привязан к user+сумме+набору начислений) → показываем
     подтверждение с суммой/комсой/рангом. Нажатие «Подтвердить» → **consume** токена (атомарно, single-use);
     повторное/залипшее нажатие → токен уже израсходован → «уже обрабатывается», без второго резерва
     (защита от double-spend/залипших кнопок, безопасность §TG).
   - На consume: атомарно `reserve_accruals` (переводим выбранные `available`→`reserved`, FIFO) + создаём
     `payouts`-строку `status=reserved` с `idempotency_key`, снапшотом кошелька, gross/commission/net.
     **Тут поток заканчивается** — фактическую отправку делает Phase 5. Кулдаун якорится на УСПЕШНОЙ отправке
     (Phase 5), не на резерве.
   - Всё в одной транзакции (DbSessionMiddleware). Идемпотентность: `idempotency_key` уникален.
6. **Никаких секретов в логи.** Адрес маскируем. Ошибки хэндлеров → `bot/alerts.py` разработчику (dev-alert),
   плюс мягкое сообщение юзеру (как `handlers/errors.py`).
7. **Залипшие кнопки/старые callback:** каждый инлайн-callback отвечает `callback.answer()` (гасит "часики");
   устаревшие/невалидные токены/состояния → мягкий ответ, без падения.

## Подход к тестам

- **service.py / repository** — реальный Postgres (testcontainers), как Phase 3/4 БД. Money-инварианты
  (баланс FIFO, reserve атомарность, коллизия UID, single-use action_token) — только на настоящей БД.
- **Хэндлеры** — вызываем функции хэндлеров напрямую с фейковыми `Message`/`CallbackQuery` (простые объекты
  с нужными полями + `AsyncMock` для `.answer`) и `FSMContext` поверх `MemoryStorage`; проверяем переходы
  состояний, тексты (по ключевым подстрокам), и что service-слой вызван с верными аргументами. Живой Bot/TG
  не поднимаем. Whitelist-middleware тестируется отдельно (уже есть).
- **Double-spend тест (обязателен):** два «подтверждения» одного action_token → ровно один payout/reserve.

## Решения владельца (2026-07-13) — ЗАФИКСИРОВАНО

- ✅ **Адрес кошелька:** только формат-валидация `0x` + 40 hex, БЕЗ новой зависимости (keccak/EIP-55 не тянем).
  Хранить адрес как введён (нормализуя регистр к нижнему для `address_hash`-lookup).
- ✅ **action_token TTL = 5 минут, одноразовый.** Consume — атомарный `UPDATE ... WHERE state='issued'`;
  на consume пере-валидируем, что резервируемый набор всё ещё `available` (защита от стейла внутри окна).
- ✅ **Семантика вывода:** поток доходит до `payouts(status=reserved)` + резерв начислений (`available`→`reserved`),
  отправку делает Phase 5. Повторный вывод блокируется наличием активного `reserved`-payout у юзера
  (не по кулдауну — кулдаун якорится на УСПЕШНОЙ отправке в Phase 5).

## Прочие допущения (владелец: сверь) — НЕ блокируют, беру дефолты
- **Тексты/язык UI:** русский, как в существующих `keyboards.py`/`rendering.py`. Кнопочный UX, минимум ручного ввода.
- **Стата по каждому UID vs только суммарно:** показываю и то, и то (бизнес 2.3). Ок?
- **Уведомления (ранг/выплата/ниже минимума):** заложу интерфейс `notify(user, event)`, но авто-рассылку
  (требует живой Bot + шедулер) подключим в Phase 5/интеграции; в тестах замокан.
- **Резерв при выводе создаёт payout `reserved` и на этом стоп** (Phase 5 добьёт до sent/confirmed). Кулдаун
  считается от успешной отправки (Phase 5), поэтому в Phase 4 после reserve повторный вывод блокируется
  наличием активного `reserved`-payout (не по кулдауну). Ок?

## Порядок задач (для плана)
1. DbSessionMiddleware + DI sessionmaker (+ тест: commit/rollback контур).
2. botui/repository расширения: upsert_user, wallet get/replace, account add/list/archive+collision, balance,
   effective_rank_now, last_success_withdrawal_at (чтения) — Postgres-тесты.
3. Wallet-флоу (валидация адреса, шифрование, история) — service + handler тесты.
4. Accounts-флоу (add/list/remove + коллизия + accrual_since) — тесты.
5. Stats (баланс/ранг/fee/след.вывод/экспирация) — тесты.
6. action_token: create/consume single-use атомарно (repository) — Postgres-тест (в т.ч. double-consume).
7. Withdraw-флоу: проверки → action_token confirm → reserve+payout(reserved) — тесты, включая double-spend.
8. Меню/навигация + регистрация роутеров в build_dispatcher + errors/dev-alert обвязка — тесты + quality gate.
