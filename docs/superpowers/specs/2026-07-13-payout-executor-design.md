# Phase 5 — Payout-executor design (chain-agnostic, mock chain only)

Статус: дизайн (автономный). Источники: `PAYOUT_SECURITY_RELIABILITY.md` §1-6, §10 (принято владельцем),
`PAYOUT_BUSINESS_LOGIC.md` §5, `DB_SCHEMA.md` (payouts/rebate_accruals/audit_events). Строится subagent-TDD.

## Граница автономии (жёстко)
- **Никакого mainnet, реальных ключей, реальных денег.** Executor строится **chain-agnostic**: интерфейс
  `ChainClient` + только `MockChainClient` в тестах. Реальную web3-реализацию + ключи + RPC добавляет
  владелец на деплое (туда же — зависимость web3/eth-account). **В Phase 5 новых зависимостей НЕ ставим.**
- Вся денежно-критичная логика (state-машина, nonce single-flight, идемпотентность, «sent но неизвестно»,
  подтверждения, реконсиляция) пишется и тестируется на моке. Это и есть ценная часть.

## Что уже готово (вход для executor)
Phase 4 оставляет выплату в `payouts(status='reserved')` с зарезервированными начислениями
(`rebate_accruals.state='reserved'`, `payout_id` проставлен), `idempotency_key`, снапшотом кошелька,
gross/commission/net. Executor **подхватывает `reserved`** и доводит до `paid`/`failed`.

## State-машина выплаты (payout_status)
```
reserved ──(> AUTO_LIMIT)──> pending_admin ──(admin release, Phase 6)──┐
   │ (<= AUTO_LIMIT)                                                    │
   ▼                                                                    ▼
submitting ──broadcast──> sent ──15 confs──> confirmed ──> paid
   │                        │
   └─(fail closed)──────────┴──> failed        cancelled (admin, Phase 6)
```
- `reserved → submitting`: только если `net >= MIN_PAYOUT` и `net > 0`; фиксируем `nonce` и `idempotency_key`
  **до** броадкаста (§4.3.1).
- `submitting → sent`: после броадкаста сохраняем `txhash` (§4.3.2).
- `sent → confirmed`: дождались **15 подтверждений** (`CONFIRMATIONS=15`, §4.5); реорг < 15 не финализируем.
- `confirmed → paid`: атомарно — начисления `reserved → paid`, `paid_at`, кулдаун якорится на `confirmed_at`
  (последняя успешная выплата, бизнес 5.1).
- любой fail-closed (газ выше потолка — отложено; RPC-неопределённость без подтверждения tx; net<min) →
  `failed`, начисления `reserved → available` (возвращаем в живые, чтобы не потерять и не переотправить вслепую).
- `> AUTO_LIMIT` ($200, §8) → `pending_admin`, executor НЕ отправляет; релиз — Phase 6 (admin).

## Инварианты (не проебать деньги)
1. **Single-flight** (§4.2): одна выплата «в полёте» на адрес. Обрабатываем строго по одной; `nonce`
   ведём в БД. `nonce = max(on-chain pending nonce, last_db_nonce + 1)`, фиксируем в записи **до** броадкаста.
2. **Никогда не переотправлять вслепую** (§4.3): при неопределённости сначала спрашиваем цепочку
   (`ChainClient.get_tx_status(txhash)` / по nonce от нашего адреса). Повторная отправка — только
   **replacement** (тот же nonce, выше газ), не новая выплата. `idempotency_key` уникален (уже в схеме).
3. **Реконсиляция на старте** (§6.1): все `submitting`/`sent` — проверяем цепочку, доводим до `paid`/`failed`,
   ничего не шлём заново вслепую.
4. **decimals = 18** (§4.6, 🔒): BEP20 USDT на BSC = 18 знаков (НЕ 6). Сумма: round down до сотых →
   в 18-decimal wei. Чистая функция `usdt_to_wei(Decimal) -> int` с исчерпывающими тестами (главный риск —
   ошибка в 10^12 раз). chainId 56, адрес контракта USDT, ERC20 `transfer` — зашиваются в РЕАЛЬНЫЙ
   `ChainClient` (деплой); мок принимает их как параметры, но логику суммы тестируем сейчас.
5. **Газ на тест-фазе НЕ вычитается из юзера** (§4.4, реш. §10) — несёт владелец; `net` остаётся как в Phase 4.
   `MAX_GAS_PRICE`/курс BNB→USDT отложены (владелец). Вернуть вычет газа — когда появится курс.
6. **Экспирация на паузе для `reserved`/`pending_admin`** (§6.2) — уже так: `expire_aged` трогает только
   `available`. Проверить/подтвердить в тестах.
7. Каждый переход — в транзакции (§5). Audit-запись на каждый переход (кто=executor, txid, nonce, сумма).

## Модули (новый пакет `bot/executor/`)
- `chain.py` — `ChainClient` Protocol (`get_nonce(address) -> int`, `send_transfer(...) -> str(txhash)`,
  `get_confirmations(txhash) -> int`, `get_tx_status(txhash) -> Literal["pending","success","failed","unknown"]`),
  `MockChainClient` (детерминированный, программируемые ответы для тестов), `usdt_to_wei(Decimal) -> int` (18 dp).
- `repository.py` — переходы: `claim_next_payable(session, auto_limit) -> Payout | None` (берёт самый старый
  `reserved`, single-flight), `route_over_limit`, `mark_submitting(nonce)`, `mark_sent(txhash)`,
  `mark_confirmed`, `finalize_paid` (accruals→paid, paid_at), `finalize_failed` (accruals→available),
  `payouts_in_flight(statuses)` для реконсиляции, `last_nonce`. Всё атомарно.
- `executor.py` — `PayoutExecutor`: `process_next(now)` (single-flight один payout сквозь машину до sent/paid),
  `poll_confirmations(now)` (sent→confirmed→paid по 15 conf), `reconcile()` (старт). Инъекция `ChainClient`,
  `now`, `sessionmaker`, `auto_limit`.
- `scheduler.py` — тонкий цикл (как accrual/scheduler): reconcile → периодически process_next + poll_confirmations.
- Config: `AUTO_LIMIT_USDT = Decimal("200")`, `CONFIRMATIONS = 15`, `PAYOUT_WALLET_ADDRESS`/`USDT_CONTRACT`/
  `BSC_CHAIN_ID=56` (используются только реальным ChainClient; в Phase 5 не обязательны).

## Тесты (money-critical, на моке + реальный Postgres для repo)
- `usdt_to_wei`: `Decimal("1") -> 10**18`, `Decimal("0.01") -> 10**16`, округление вниз до сотых, НЕ 6 знаков.
- single-flight: при уже «в полёте» выплате `claim_next_payable` не берёт вторую.
- nonce: фиксируется до броадкаста; `max(on-chain, db+1)`.
- «sent но неизвестно»: RPC-таймаут после броадкаста → реконсиляция видит tx в цепочке (мок) → НЕ шлёт вторую,
  доводит до paid. И обратный кейс: tx нет в цепочке → безопасный replacement/же fail, без двойной оплаты.
- confirmations: `sent` при <15 не финализируется; при ≥15 → confirmed → paid.
- paid: начисления `reserved→paid`, кулдаун якорится (последующий `prepare_withdrawal` учитывает last success).
- failed: начисления `reserved→available` (возвращены, не потеряны).
- over AUTO_LIMIT: `net > 200` → `pending_admin`, executor не отправляет.
- реконсиляция на старте доводит `submitting`/`sent` без слепой переотправки.

## Открытые вопросы / допущения (владелец: сверь) — беру дефолты
- **Chain-agnostic + MockChainClient, без web3-зависимости в Phase 5.** Реальный клиент + ключи + RPC — деплой.
- **AUTO_LIMIT = $200** (§8) → `pending_admin` (релиз в Phase 6). Дефолт.
- **Газ не вычитается на тест-фазе** (§10). Дефолт.
- **Уведомление юзеру о выплате (txid)** — интерфейс `notify`, авто-отправка при живом Bot (интеграция/деплой);
  в тестах замокан.
- **Вынесенный signer/HSM** (§2) — не в Phase 5 (ключа тут вообще нет). Отложено на деплой.

## Порядок задач (для плана)
1. `usdt_to_wei` + `ChainClient` Protocol + `MockChainClient` (чистое + мок, юнит-тесты decimals).
2. executor/repository: claim_next (single-flight), state-переходы, finalize paid/failed (accruals), last_nonce.
3. executor.process_next: reserved→(pending_admin | submitting→sent), nonce до броадкаста, over-limit.
4. confirmations: sent→confirmed→paid (15), accruals→paid, кулдаун-якорь.
5. «sent но неизвестно» + reconcile на старте (никакой слепой переотправки).
6. failed-путь (accruals→available) + audit на переходы.
7. scheduler + config (AUTO_LIMIT/CONFIRMATIONS) + quality gate + автоном-статус.
