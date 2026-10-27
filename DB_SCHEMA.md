# Схема БД (PostgreSQL) — черновик

Собрана из [PAYOUT_BUSINESS_LOGIC.md](PAYOUT_BUSINESS_LOGIC.md),
[PAYOUT_SECURITY_RELIABILITY.md](PAYOUT_SECURITY_RELIABILITY.md),
[PAYOUT_ARCHITECTURE.md](PAYOUT_ARCHITECTURE.md).

Цель — чтобы критичные инварианты держала **сама БД** (constraints/indexes), а не
только код: не заплатить дважды, один активный кошелёк, uid у одного владельца,
адрес-снапшот в выплате.

## Соглашения

- Деньги — `NUMERIC(38, 18)` (хватает на 18 знаков BSC-USDT; никаких float).
- Время — `TIMESTAMPTZ` в UTC; «сутки/месяц» бизнес-логики считаем в `PAYOUT_TIMEZONE`
  (UTC+8) на уровне кода, в БД храним абсолютный момент.
- Enum-ы — как PG `ENUM` или `TEXT + CHECK` (ниже пишу как enum для читаемости).
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` подразумевается почти везде.

---

## 1. Идентичность

### users
```sql
CREATE TABLE users (
  id                BIGINT PRIMARY KEY,          -- = telegram_user_id (натуральный ключ)
  role              user_role NOT NULL DEFAULT 'user',   -- user | dev | admin
  status            user_status NOT NULL DEFAULT 'active', -- active | blocked | manual_review
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### wallets
Один активный BEP20-адрес на пользователя; смена = новая строка, старая → `replaced`.
Полный адрес шифруется на уровне приложения; для поиска дублей — HMAC-хэш.
```sql
CREATE TABLE wallets (
  id                BIGSERIAL PRIMARY KEY,
  user_id           BIGINT NOT NULL REFERENCES users(id),
  chain             TEXT NOT NULL DEFAULT 'BEP20' CHECK (chain = 'BEP20'),
  address_encrypted BYTEA NOT NULL,              -- полный адрес, зашифрован (Fernet)
  address_hash      BYTEA NOT NULL,              -- HMAC(pepper, normalized_addr) для дублей
  address_masked    TEXT NOT NULL,               -- 0x1234...abcd для UI/логов
  status            wallet_status NOT NULL DEFAULT 'active', -- active|replaced|disabled|admin_hold
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  replaced_at       TIMESTAMPTZ
);
-- 🔒 не больше одного активного кошелька на пользователя:
CREATE UNIQUE INDEX uq_wallet_active ON wallets(user_id) WHERE status = 'active';
CREATE INDEX ix_wallet_hash ON wallets(address_hash);   -- risk-flag: один адрес у многих
```

### exchange_accounts (UID)
Несколько UID на пользователя; пара `(exchange, uid)` уникальна глобально (first-come).
```sql
CREATE TABLE exchange_accounts (
  id                BIGSERIAL PRIMARY KEY,
  user_id           BIGINT NOT NULL REFERENCES users(id),
  exchange          TEXT NOT NULL,               -- bybit | okx | ... (совпадает с ExchangeSpec.code)
  uid               TEXT NOT NULL,
  status            account_status NOT NULL DEFAULT 'active', -- active|blocked|archived
  accrual_since     DATE NOT NULL,               -- = REBATE_EPOCH (2026-07-10) или позже (бизнес 3.2)
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_exchange_uid UNIQUE (exchange, uid)   -- 🔒 uid у одного владельца
);
CREATE INDEX ix_exch_acc_user ON exchange_accounts(user_id);
```

---

## 2. Денежный реестр (ядро)

### rebate_accruals
Одна строка = рефбек за **один день** по одному UID. Ставка/ранг/комса **заморожены**
в момент начисления (бизнес-правило 5.2). Доступность — с `available_at` (D+1),
сгорание — по `expires_at` (D+30 + grace).
```sql
CREATE TABLE rebate_accruals (
  id                BIGSERIAL PRIMARY KEY,
  user_id           BIGINT NOT NULL REFERENCES users(id),
  account_id        BIGINT NOT NULL REFERENCES exchange_accounts(id),
  accrual_date      DATE NOT NULL,               -- день D в PAYOUT_TIMEZONE
  asset             TEXT NOT NULL DEFAULT 'USDT',

  amount_usdt       NUMERIC(38,18) NOT NULL,     -- gross рефбек за день (по всем записям биржи)
  effective_rank    SMALLINT NOT NULL,           -- 0..4, ранг пользователя в этот день (заморожен)
  fee_pct           NUMERIC(6,4) NOT NULL,       -- 0.10 / 0.07 / 0.03 / 0.00 (заморожен)
  commission_usdt   NUMERIC(38,18) NOT NULL,     -- amount * fee_pct (заморожен)
  net_usdt          NUMERIC(38,18) NOT NULL,     -- amount - commission (доля юзера, до газа)

  state             accrual_state NOT NULL DEFAULT 'accruing',
                    -- accruing | available | reserved | paid | expired
  available_at      TIMESTAMPTZ NOT NULL,        -- начало дня D+1 (нельзя вывести текущий день)
  expires_at        TIMESTAMPTZ NOT NULL,        -- начало дня D+30 (+ grace за простой)
  payout_id         BIGINT REFERENCES payouts(id),  -- какой выплатой оплачено (если оплачено)

  raw_records_count INT NOT NULL DEFAULT 0,
  adapter_notes     TEXT,
  finalized         BOOLEAN NOT NULL DEFAULT FALSE,  -- день закрыт, сумма не меняется
  fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_accrual_day UNIQUE (account_id, accrual_date)  -- идемпотентный upsert за день
);
-- 🔒 одно начисление — максимум в одной выплате (защита от двойной оплаты):
CREATE UNIQUE INDEX uq_accrual_payout ON rebate_accruals(id) WHERE payout_id IS NOT NULL;
-- быстрый FIFO-выбор доступного к выводу:
CREATE INDEX ix_accrual_available ON rebate_accruals(user_id, accrual_date)
  WHERE state = 'available';
-- джоб экспирации:
CREATE INDEX ix_accrual_expiry ON rebate_accruals(expires_at) WHERE state = 'available';
```
Замечания:
- Пока день D открыт — строка `accruing`, `finalized=false`, апсертится поллером
  (сумма растёт). После закрытия дня (в D+1) → `finalized=true`, `state='available'`.
- Комса заморожена: пересчёт ранга задним числом строку не трогает.
- `< $10/мес` → строки этого месяца имеют `effective_rank=0`; вывод по ним недоступен,
  сгорают по `expires_at` (бизнес 4.4).

### rank_history
Достигнутый ранг за календарный месяц. Эффективный ранг = max за 3 последних месяца
(бизнес 4.3) считается в коде из этих строк.
```sql
CREATE TABLE rank_history (
  id                BIGSERIAL PRIMARY KEY,
  user_id           BIGINT NOT NULL REFERENCES users(id),
  period_month      DATE NOT NULL,               -- первое число месяца
  achieved_rank     SMALLINT NOT NULL,           -- 0..4 по gross за месяц
  gross_month_usdt  NUMERIC(38,18) NOT NULL,
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_rank_month UNIQUE (user_id, period_month)
);
```

---

## 3. Выплаты

### payouts
Одна выплата забирает все доступные начисления юзера (FIFO), с адрес-снапшотом.
```sql
CREATE TABLE payouts (
  id                BIGSERIAL PRIMARY KEY,
  user_id           BIGINT NOT NULL REFERENCES users(id),

  -- снапшот кошелька на момент создания выплаты (не ссылка на «текущий»!):
  wallet_id         BIGINT NOT NULL REFERENCES wallets(id),
  address_encrypted BYTEA NOT NULL,
  address_masked    TEXT NOT NULL,

  covered_from      DATE NOT NULL,               -- min(accrual_date)
  covered_to        DATE NOT NULL,               -- max(accrual_date)
  gross_usdt        NUMERIC(38,18) NOT NULL,
  commission_usdt   NUMERIC(38,18) NOT NULL,     -- ранговая комса (заморожена)
  commission_override_usdt NUMERIC(38,18),       -- ручное занижение админом (только вниз); NULL = нет
  overridden_by     BIGINT REFERENCES users(id), -- admin, применивший занижение
  override_reason   TEXT,                         -- обязательна при занижении (audit)
  gas_bnb           NUMERIC(38,18),              -- пока NULL (газ на тест-фазе несёт владелец)
  gas_usdt          NUMERIC(38,18) NOT NULL DEFAULT 0,
  bnb_usdt_rate     NUMERIC(38,18),
  net_usdt          NUMERIC(38,18) NOT NULL,     -- gross - COALESCE(commission_override, commission) - gas_usdt

  status            payout_status NOT NULL DEFAULT 'reserved',
                    -- reserved|pending_admin|submitting|sent|confirmed|paid|failed|cancelled|skipped_below_min
  nonce             BIGINT,                      -- EVM nonce, фиксируется до броадкаста
  txhash            TEXT,
  idempotency_key   TEXT NOT NULL,               -- 🔒 стабильный ключ операции
  action_token      UUID,                        -- какая кнопка создала (см. action_tokens)

  requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_by       BIGINT REFERENCES users(id), -- admin, если > AUTO_LIMIT
  approved_at       TIMESTAMPTZ,
  sent_at           TIMESTAMPTZ,
  confirmed_at      TIMESTAMPTZ,
  paid_at           TIMESTAMPTZ,
  failed_reason     TEXT,

  CONSTRAINT uq_payout_idem UNIQUE (idempotency_key)  -- 🔒 не создать дубль выплаты
);
-- один pending nonce на кошелёк (single-flight на уровне БД):
CREATE UNIQUE INDEX uq_payout_active_nonce ON payouts(nonce)
  WHERE status IN ('submitting','sent');
CREATE INDEX ix_payout_user ON payouts(user_id, requested_at DESC);  -- кулдаун: последний успешный
```
- Связь «выплата → начисления» — через `rebate_accruals.payout_id` (одно начисление в
  одной выплате). `covered_from/to` денормализованы для UI.
- Кулдаун считается от последней успешной выплаты пользователя (max `paid_at`/`sent_at`);
  при желании денормализовать `users.last_payout_at`.
- `> $200 (AUTO_LIMIT)` → создаётся в `pending_admin`; после `approved_by/at` → в очередь
  executor. В `reserved`/`pending_admin` связанные начисления **не сгорают** (6.2).

---

## 4. Операционка и защита

### audit_events (append-only)
```sql
CREATE TABLE audit_events (
  id                BIGSERIAL PRIMARY KEY,
  actor             TEXT NOT NULL,               -- 'user:123' | 'admin:1' | 'system' | 'executor'
  action            TEXT NOT NULL,               -- wallet.change | payout.create | payout.sent | ...
  entity_type       TEXT NOT NULL,
  entity_id         TEXT,
  old_value         JSONB,                       -- маскированное
  new_value         JSONB,                       -- маскированное
  amount_usdt       NUMERIC(38,18),
  txhash            TEXT,
  nonce             BIGINT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- запретить UPDATE/DELETE (append-only) — триггером или ролью.
```

### telegram_updates (дедуп at-least-once)
```sql
CREATE TABLE telegram_updates (
  update_id         BIGINT PRIMARY KEY,          -- 🔒 идемпотентность обработки апдейтов
  processed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);  -- периодически чистить старые
```

### action_tokens (одноразовые кнопки)
```sql
CREATE TABLE action_tokens (
  token             UUID PRIMARY KEY,
  user_id           BIGINT NOT NULL REFERENCES users(id),
  action_type       TEXT NOT NULL,               -- 'withdraw' | 'wallet_change_confirm' | ...
  payload           JSONB,
  state             token_state NOT NULL DEFAULT 'issued', -- issued | used | expired
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  used_at           TIMESTAMPTZ,
  expires_at        TIMESTAMPTZ NOT NULL
);  -- 🔒 защита от двойных тапов/устаревших кнопок: атомарный issued→used
```

### service_availability (grace при простое)
Интервалы недоступности сервиса; экспирация продлевается на суммарный простой (6.2).
```sql
CREATE TABLE service_availability (
  id                BIGSERIAL PRIMARY KEY,
  down_from         TIMESTAMPTZ NOT NULL,
  down_to           TIMESTAMPTZ,                 -- NULL пока длится
  reason            TEXT
);
```

### system_flags (админ-тумблеры, kill-switch)
```sql
CREATE TABLE system_flags (
  key               TEXT PRIMARY KEY,            -- 'payouts_paused' | ...
  value             JSONB NOT NULL,
  updated_by        BIGINT REFERENCES users(id),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 5. Как БД держит ключевые правила

| Правило | Механизм в БД |
|---------|---------------|
| uid у одного владельца | `UNIQUE (exchange, uid)` |
| один активный кошелёк | partial `UNIQUE (user_id) WHERE status='active'` |
| не платить дважды за начисление | `rebate_accruals.payout_id` + partial unique; переходы под `SELECT … FOR UPDATE` |
| не создать дубль выплаты | `payouts.idempotency_key UNIQUE` |
| single-flight nonce | partial `UNIQUE (nonce) WHERE status IN ('submitting','sent')` |
| адрес-снапшот | `payouts.address_encrypted` (копия, не ссылка на текущий) |
| двойные тапы / устаревшие кнопки | `action_tokens` (issued→used атомарно) |
| дедуп апдейтов TG | `telegram_updates PK(update_id)` |
| заморозка комсы | `effective_rank/fee_pct/commission_usdt` на строке начисления |
| грейс экспирации | `service_availability` + пауза для `reserved/pending_admin` |
| аудит | `audit_events` append-only |

---

## 6. Роли БД (по PAYOUT_ARCHITECTURE.md)

- `bot_app`: users/wallets/exchange_accounts/rebate_accruals (чтение+запись заявок),
  создание `payouts` в `reserved/pending_admin`. Без права двигать в `sent/paid`.
- `accrual_worker`: запись `rebate_accruals`, `rank_history`, `service_availability`.
- `payout_exec`: только выплаты в исполнимых статусах — переходы
  `pending_admin/reserved → submitting → sent → confirmed → paid`; запись nonce/txhash.
- `admin_readonly`: отчёты.
- migration-роль отдельно. Минимальные GRANT, RLS как default-deny слой.

---

## 7. Открытые вопросы к схеме ❓

1. `exchange` — PG enum или свободный текст (проще добавлять биржи)? Предлагаю TEXT+FK
   на справочник бирж.
2. Хранить ли **сырой** дневной разбор рефбека (по токенам/сделкам) или только дневной
   итог `amount_usdt`? Для аудита полезно, но это персональные данные — в старом
   документе решили raw не тащить. Предлагаю только итог + `raw_records_count`.
3. Денормализовать `users.last_payout_at` и `users.current_effective_rank` для скорости
   UI, или считать на лету? Предлагаю считать на лету, денорм — если упрёмся в перф.
4. Партиционирование `rebate_accruals`/`audit_events` по времени — когда объём вырастет
   (пока не нужно).
5. **Cold-start** (бизнес 3.2) — РЕШЕНО: эпоха `REBATE_EPOCH = 2026-07-10` + бэкфилл с
   реальными историческими датами и обычной 30-дневной экспирацией. `accrual_since =
   REBATE_EPOCH` (или позже). Нужны: конфиг-константа `REBATE_EPOCH` и **backfill-джоб**
   (при подключении UID тянет рефбек по дням); начисления старше 30 дней создаются сразу
   в состоянии `expired`. Бэкфилл-начисления учитываются в `rank_history`, даже если
   `expired` для выплаты.
6. **Ручное занижение комсы админом** (бизнес 1.1) — сейчас заложено полями
   `commission_override_usdt/overridden_by/override_reason` на `payouts` (разовая скидка на
   конкретную выплату). Если понадобится **персональная пониженная ставка на будущее** —
   добавить таблицу `commission_overrides(user_id, override_fee_pct, effective_from,
   created_by, reason)`, которую читает accrual-worker при заморозке ставки. Решение — за
   заказчиком.
