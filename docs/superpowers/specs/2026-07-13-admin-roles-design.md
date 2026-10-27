# Phase 6 — Admin / roles design (final phase)

Статус: дизайн (автономный). Источники: `PAYOUT_SECURITY_RELIABILITY.md` §8, `PAYOUT_BUSINESS_LOGIC.md` §1/§1.1,
`DB_SCHEMA.md`. Строится subagent-TDD. Модифицирует уже влитый accrual-worker (Phase 3) — аккуратно, с ревью.

## Решения владельца (2026-07-13) — ЗАФИКСИРОВАНО
- **Занижение комсы = (B) персональная ставка «с момента N».** Админ ставит юзеру `fee_pct` с датой;
  применяется к начислениям, **замороженным с этой даты вперёд** (уже замороженные дни не пересчитываются).
  **«Только вниз»:** эффективная ставка дня = `min(ранговая ставка, override)` — оверрайд лишь уменьшает комсу.
- **Релиз > $200:** `pending_admin → reserved` (executor подхватит на след. цикле).
- **Админка = тот же бот**, команды/кнопки под ролью `admin` (роли `user/dev/admin` уже в схеме).
- **Споры по UID:** админ переназначает/блокирует `(exchange, uid)`.

## Что уже есть
- `users.role` (user/dev/admin), `users.status` (active/blocked/manual_review) — Phase 2.
- `system_flags` (kill-switch хранилище), `audit_events`, `payouts` (`commission_override_usdt`/`overridden_by`/
  `override_reason` — для разовой скидки), `exchange_accounts` (uid ownership).
- Accrual-worker (Phase 3) морозит `fee_pct` на строке — точка интеграции для (B).
- Executor (Phase 5): `pending_admin` ждёт релиза; process_next берёт `reserved`.
- Botui (Phase 4): whitelist-middleware, DbSessionMiddleware, cabinet-роутеры.

## Модель данных (изменения)
- **Миграция:** на `users` добавить `commission_override_pct Numeric(6,4) NULL`, `commission_override_since Date NULL`.
  (Колонки на users, без новой таблицы — один владелец; история изменений идёт в `audit_events`.)
  Alembic-миграция; downgrade убирает обе колонки.
- Kill-switch: строка в `system_flags` с ключом `payouts_paused` → `{"paused": true/false}`.

## Money-интеграция (B) — модификация accrual-worker (Phase 3)
- В `bot/accrual/` fee-расчёт дня: если у юзера активен оверрайд (`commission_override_pct` задан и
  `commission_override_since <= D`) → `fee = min(fee_pct(rank), override_pct)`; иначе как сейчас `fee_pct(rank)`.
- Чистая функция `effective_fee(rank, override_pct | None) -> Decimal` в `bot/payout/` (тестируемо изолированно);
  worker читает оверрайд юзера при заморозке дня и передаёт в неё.
- Заморозка сохраняется: применяется в момент начисления, задним числом не меняем. Бэкфилл старых дней
  (D < since) оверрайд НЕ получает (гейт по `since`).
- **«Только вниз» гарантируется `min()`** на этапе заморозки — даже если админ по ошибке введёт больше ранга,
  юзер платит не больше рангового. Плюс валидация при вводе: `0 <= override_pct <= 1`.

## Роли и гейтинг
- `bot/botui/admin/` (новый суб-пакет) или `bot/admin/`. Роутер `admin` с фильтром `IsAdmin` (проверяет
  `users.role == 'admin'` через сессию по `from_user.id`). Не-админ — тихо игнор (как whitelist) или мягкий отказ.
- Whitelist уже глобальный; admin — доп. гейт поверх.

## Админ-возможности (команды/кнопки, все с audit + причиной где деньги)
1. **`/admin` меню** (только admin).
2. **Релиз крупных выплат:** список `pending_admin` → «Одобрить» → `pending_admin → reserved` (+ опц. разовое
   занижение комсы на этой выплате: `commission_override_usdt`, пересчёт net вверх, `overridden_by`/`override_reason`);
   «Отклонить» → `cancelled`, начисления `reserved → available`.
3. **Персональная ставка (B):** `set fee_pct` юзеру (0..ранговая) + причина → пишем `commission_override_pct`/
   `_since=today`, audit. «Сбросить» → null.
4. **Споры по UID:** переназначить `(exchange, uid)` другому юзеру (archive у старого, create у нового) или
   заблокировать пару (`account_status='blocked'`). Только будущие начисления; уже выплаченное не отзывается.
5. **Блок/разблок юзера:** `users.status = blocked/active`. Заблокированный не может вывод/добавление.
6. **Kill-switch:** пауза/снятие всех выплат (`system_flags.payouts_paused`). Executor `process_next` и botui
   `withdraw` проверяют флаг → при паузе не отправляют/не резервируют (мягкое сообщение).
7. **Просмотр статистики** (light): суммарно по юзерам/выплатам (без финансовых действий).

## Интеграция в существующий код (money-critical, с ревью)
- **accrual-worker:** применить `effective_fee` (см. выше).
- **executor.process_next:** в начале — если `payouts_paused` → return (kill-switch); blocked-юзер — не отправляем
  (его выплата не должна была создаться, но double-check).
- **botui.withdraw.prepare_withdrawal:** если юзер `blocked` или `payouts_paused` → blocked-причина.

## Открытые вопросы / допущения (владелец: сверь) — беру дефолты
- **Первый admin:** назначается вручную (env `ADMIN_TELEGRAM_ID` или разовый seed/SQL), не через бота
  (некому назначать первого). Дефолт: `DEVELOPER_TELEGRAM_ID`/новый `ADMIN_TELEGRAM_IDS` env → роль admin при upsert.
- **RLS/DB-роли** (§ из ТЗ) — app-level гейтинг (IsAdmin) в Phase 6; Postgres RLS отложить на деплой (не app-логика).
- **Risk-flags** (§8) — интерфейс/пометки, авто-флаги (новый юзер, смена кошелька, скачок) — облегчённо/отложено.
- **Разовое занижение (A)** на выплате при релизе — включаю как бонус (поля уже есть), сверх персональной (B).

## Порядок задач (для плана)
1. Миграция users (override_pct/since) + `effective_fee` чистая функция + тесты.
2. accrual-worker интеграция override (min at freeze) + тесты (в т.ч. since-гейт, заморозка, только-вниз).
3. Роль-гейт `IsAdmin` + `/admin` меню + admin-репозиторий (чтения/записи) + kill-switch flag get/set.
4. Релиз/отклонение `pending_admin` (+ разовое занижение) — repo + handler + тесты.
5. Персональная ставка set/clear (B) + UID reassign/block + block/unblock user — handlers + audit + тесты.
6. Kill-switch + blocked-user проверки в executor.process_next и botui.withdraw — тесты.
7. Wiring (admin-роутер в build_dispatcher; ADMIN env → роль) + gate + автоном-статус.
