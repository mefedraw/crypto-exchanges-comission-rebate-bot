# Архитектура заявок и выплат рефбека

Черновик безопасной архитектуры для системы, где пользователи заранее оставляют
заявки на выплату, бот считает рефбек за месяц, владелец подтверждает итог, а
отдельный payout-контур отправляет средства на BEP20-кошельки.

## Цели

- Пользователь с 27 числа по последний день месяца оставляет заявку: биржа,
  UID, BEP20-адрес кошелька.
- Один Telegram-пользователь может оставить несколько заявок на разные UID,
  в том числе на одной бирже.
- 1 числа бот считает рефбек за прошедший месяц, собирает сводку для владельца
  и готовит payout batch.
- Владелец видит статистику, сумму к пополнению, конфликтные заявки и список
  выплат.
- После пополнения payout-источника владелец подтверждает отправку, а система
  рассылает выплаты.

## Главный принцип

Расчёт рефбека и перевод денег должны быть разными контурами.

Обычный Telegram-бот может принимать заявки и читать affiliate API. Ключи,
которые умеют выводить или переводить средства, должны быть доступны только
изолированному payout-executor. Если взломают Telegram-бота, злоумышленник не
должен получить возможность сразу вывести деньги.

## Роли

### Пользователь

- Создаёт заявку на выплату.
- Может иметь несколько UID на одной бирже.
- Может иметь один активный BEP20-кошелёк.

### Владелец

- Получает сводку заявок и расчётов.
- Разрешает конфликты по UID.
- Подтверждает batch выплат.
- Пополняет payout-источник.
- Даёт финальное подтверждение на отправку.

### Bot app

- Telegram-интерфейс.
- Приём и валидация заявок.
- Расчёт rebate через read-only affiliate API.
- Подготовка batch, но без доступа к withdrawal/transfer API keys.

### Payout executor

- Отдельный процесс/контейнер.
- Имеет доступ к withdrawal/transfer API key.
- Принимает только утверждённый batch из БД или внутренней очереди.
- Проверяет лимиты, idempotency, баланс, адреса и chain.
- Выполняет выплаты и пишет txid/status.

## Календарь

- `PAYOUT_TIMEZONE`: лучше явно `Europe/Moscow` или UTC, но один стандарт для
  всей системы.
- Окно заявок: с 27 числа 00:00 до последнего дня месяца 23:59:59.
- Заявка в окне относится к текущему календарному месяцу.
- 1 числа в заданное время бот считает предыдущий месяц.
- После запуска расчёта заявки за месяц замораживаются.
- Поздние заявки не попадают в batch автоматически. Их можно обработать только
  вручную или перенести на следующий цикл.

## Сущности

### users

- `telegram_user_id`
- `username`
- `first_seen_at`
- `status`: active, blocked, manual_review
- risk flags

### wallets

Для MVP у пользователя один активный BEP20-кошелёк. Новый адрес не добавляется
как второй payout-вариант, а заменяет текущий активный адрес до freeze. Все
замены пишутся в audit log.

- `id`
- `telegram_user_id`
- `chain`: только `BEP20` для MVP
- `address_hash`: HMAC нормализованного адреса для поиска дублей
- `address_encrypted`: зашифрованный полный адрес
- `address_masked`: например `0x1234...abcd`
- `status`: pending_confirm, active, disabled, admin_hold
- `created_at`, `confirmed_at`, `last_used_at`

Полный адрес не хранить в логах. В UI показывать полный адрес только при
подтверждении пользователем и владельцем; в остальных местах маска.

Ограничение БД: не больше одного активного кошелька на `telegram_user_id + chain`.

### uid_claims

Связка пользователя с UID.

- `exchange`
- `uid`
- `telegram_user_id`
- `status`: pending, accepted, conflict, rejected, archived
- `first_claimed_at`
- `accepted_by_owner_at`
- `notes`

Рекомендация: уникальность активной связки `exchange + uid + period`. Если тот же UID
пытается заявить другой Telegram-пользователь, заявка уходит в conflict и не
попадает в автоматическую выплату.

### payout_requests

Заявка на конкретный месяц.

- `period_month`: `YYYY-MM`
- `exchange`
- `uid`
- `telegram_user_id`
- `wallet_id`
- `status`: draft, submitted, frozen, conflict, calculated, included, paid,
  rejected, expired
- `submitted_at`
- `frozen_at`

Уникальность для MVP: один активный payout request на
`period_month + exchange + uid + telegram_user_id`. Если пользователь хочет
сменить кошелёк до закрытия окна, он меняет свой единственный активный кошелёк,
а заявка при freeze получает snapshot актуального адреса.

### rebate_calculations

Результат расчёта по UID.

- `period_month`
- `exchange`
- `uid`
- `asset`
- `amount`
- `source`
- `raw_records_count`
- `calculated_at`
- `adapter_notes`

Все суммы только Decimal. Разные активы не складывать.

### payout_batches

- `period_month`
- `status`: calculating, ready_for_review, approved, funded, executing,
  partially_sent, sent, failed, cancelled
- `batch_hash`: hash списка выплат, сумм, адресов и policy snapshot
- `total_by_asset`
- `created_at`
- `approved_by_owner_at`
- `executed_at`

### payout_items

- `batch_id`
- `payout_request_id`
- `asset`: для MVP лучше `USDT_BEP20`
- `amount`
- `wallet_address_hash`
- `wallet_address_encrypted_snapshot`
- `status`: pending, executing, sent, confirmed, failed, skipped
- `exchange_withdrawal_id`
- `txid`
- `error`
- `idempotency_key`

Нельзя строить выплаты напрямую из текущих wallet-строк во время исполнения.
Адрес должен быть snapshot из утверждённого batch, иначе смена кошелька после
approval может подменить направление выплаты.

### audit_events

Append-only журнал:

- кто сделал действие
- что изменил
- old/new values в маскированном виде
- request id / batch id / payout item id
- IP/host/service
- timestamp

Для финансового контура audit log должен быть обязательным, а не "debug".

## Бизнес-логика заявок

### Один Telegram-пользователь, несколько UID

Разрешить. Каждая пара `exchange + uid + period_month` становится отдельной
заявкой. При выплате можно агрегировать несколько payout items на один кошелёк,
но только если:

- один и тот же пользователь;
- один и тот же chain;
- один и тот же asset;
- batch ещё не утверждён.

Для MVP можно не агрегировать, а платить отдельными item-ами. Так проще аудит.

### Один UID, несколько Telegram ID

Не платить автоматически.

Правило:

1. Первый claimant не получает вечное право автоматически.
2. Если появляется второй claimant на тот же `exchange + uid`, все активные
   заявки по этому UID получают статус `conflict`.
3. Владелец вручную выбирает один из вариантов:
   - принять одного claimant;
   - отклонить всех;
   - сделать ручную выплату вне системы;
   - пометить UID как permanently blocked до выяснения.
4. Пока конфликт не решён, UID не входит в payout batch.

Идеально иметь доказательство владения UID. Если биржа не даёт API-механизма
подтверждения связи Telegram -> UID, всё равно не стоит автоматизировать спор:
это зона owner review.

### Один кошелёк у пользователя

Для MVP не делаем адресную книгу с несколькими payout-адресами. У пользователя
есть один активный BEP20-кошелёк, который используется для всех его заявок.

Рекомендуемые правила:

- Пользователь добавляет кошелёк.
- Бот показывает полный адрес и просит явное подтверждение.
- Адрес нормализуется и проверяется как EVM/BEP20 address.
- Chain не вводится свободным текстом, только фиксированная кнопка `BEP20`.
- Смена кошелька после freeze запрещена без owner override.
- Смена кошелька до freeze разрешена, включая последние часы окна заявок.
  Риск тут не технический, а операционный: если Telegram пользователя украли
  перед закрытием окна, атакующий может заменить адрес. Поэтому такие замены
  подсвечиваем владельцу risk flag, но не блокируем автоматически.
- Один и тот же кошелёк у разных Telegram-пользователей не запрещать жёстко,
  но помечать risk flag для владельца.

## Что выплачивать

Для MVP выбран один payout asset: `USDT` в сети `BEP20`.

Причина: многие биржи возвращают комиссии в разных активах. Например BitMart
futures может вернуть BTC/USDT/ETH. Автоматически конвертировать BTC/ETH в USDT
опасно, потому что потребуется trade permission или отдельная логика курса.

MVP policy:

- `USDT` rebate -> автоматическая выплата `USDT_BEP20`.
- non-USDT rebate -> показывать владельцу отдельно как "manual/non-payable".
- Пользователю выплачивается 100% USDT-суммы, которую возвращает API, с учётом
  минималки и fee policy ниже.
- Withdrawal fee оплачивает владелец.
- Минимальная сумма автоматической выплаты: `10 USDT`.
- Суммы ниже `10 USDT` не выплачиваются автоматически и получают статус
  `skipped_below_minimum`. В сводке владельцу они видны отдельно.
- Конвертацию non-USDT в USDT в MVP не автоматизируем.

Округление нужно зафиксировать отдельно под требования payout-source биржи.
По умолчанию нельзя округлять вверх. Если API вернул больше знаков, чем
разрешает withdrawal endpoint, сумму округляем вниз до допустимого precision.

Все эти правила должны попадать в batch snapshot, чтобы через месяц было понятно,
почему выплачена именно такая сумма.

## Расчёт 1 числа

1. Scheduler запускает monthly job.
2. Bot замораживает заявки предыдущего месяца.
3. Конфликтные UID исключаются из auto payout.
4. Для каждой биржи и UID бот вызывает adapter `get_commission`.
5. Результаты сохраняются в `rebate_calculations`.
6. Создаётся draft `payout_batch`.
7. Владелец получает сводку:
   - количество заявок;
   - количество уникальных UID;
   - конфликты;
   - нулевые начисления;
   - суммы по биржам;
   - суммы по активам;
   - auto-payable total в `USDT_BEP20`;
   - non-payable/manual assets;
   - список новых или подозрительных кошельков.

## Approval flow

Одного клика "перевести" мало. Нужна безопасная цепочка:

1. `Review`: владелец смотрит batch.
2. `Resolve conflicts`: конфликтные UID вручную принять/отклонить.
3. `Approve batch`: бот показывает batch hash, total, число получателей,
   max payout, список risk flags.
4. `Fund`: владелец пополняет payout-source на MEXC.
5. `Balance check`: executor проверяет, что хватает средств и fee.
6. `Final authorize`: владелец финально подтверждает операцию.
7. `Timelock`: короткая задержка, например 5-15 минут, с кнопкой отмены.
8. `Execute`: payout-executor отправляет выплаты.
9. `Reconcile`: executor сверяет статусы/txid и формирует итоговый отчёт.

Для финального подтверждения желательно использовать не только Telegram-кнопку.
Если Telegram аккаунт владельца украдут, кнопка становится точкой слива денег.

Варианты для будущего усиления:

- TOTP-код владельца.
- Ручной запуск payout-executor на сервере.
- Аппаратный ключ/WebAuthn в будущей web-admin панели.

Для MVP второй approver не нужен: финальное решение принимает владелец.

## Idempotency и защита от двойных выплат

Для каждого payout item нужен стабильный `idempotency_key`, например hash:

`period_month + exchange + uid + telegram_user_id + wallet_snapshot_hash + asset + amount`

Правила:

- Перед отправкой item переводится `pending -> executing` в транзакции БД.
- Нельзя отправить item, если уже есть `exchange_withdrawal_id` или `txid`.
- При timeout нельзя сразу повторять вывод. Сначала проверить статус на бирже.
- Повторный запуск executor должен продолжать batch, а не начинать заново.
- Если биржа вернула txid, item считается `sent`, даже если подтверждение сети
  ещё не пришло.
- `paid/confirmed` ставить только после reconciliation.

## Ошибки и ручные сценарии

- Биржа не отдала расчёт: batch получает warning; выплаты по этой бирже не
  выполняются.
- Недостаточно средств: executor не начинает batch.
- Ошибка одного item: batch становится `partially_sent`; оставшиеся можно
  продолжить только после owner review.
- Подозрительный кошелёк: item `skipped` до ручного решения.
- Пользователь ошибся адресом: если batch уже executed, система не может
  "отменить" blockchain transfer. Это должно быть явно написано в UI.

## Защита кода и репозитория

- Репозиторий private.
- `.env`, ключи, дампы БД, backup-файлы и service credentials не попадают в git.
- Включить secret scanning: например gitleaks/detect-secrets в pre-commit и CI.
- CI не должен иметь production withdrawal secrets.
- Deploy key только read-only, если сервер сам делает pull.
- Branch protection для main/master.
- Зависимости пиновать, регулярно обновлять, прогонять audit.
- Docker image собирать без секретов в build args. Если секрет нужен на build
  этапе, использовать build secrets, а не ARG/ENV.
- Не логировать сырые exchange headers, request bodies с секретами, полные
  кошельки и персональные данные.

## Защита сервера

Минимальная схема для VPS:

- SSH только по ключу.
- Root login disabled.
- Парольный SSH disabled.
- Firewall: входящий только SSH, лучше через WireGuard/Tailscale.
- Telegram bot работает long polling, входящий HTTP порт не нужен.
- Docker containers non-root.
- `read_only: true`, где возможно.
- `cap_drop: [ALL]`, `no-new-privileges`.
- Не монтировать Docker socket внутрь контейнеров.
- PostgreSQL не публиковать наружу.
- DB и bot в приватной docker network.
- Автообновления security patches или регулярное окно обновлений.
- Отдельный Unix user для сервиса.
- Логи с ротацией.
- Мониторинг падений, необычных payout attempts, auth errors.

Payout-executor лучше запускать отдельным контейнером, который:

- не имеет Telegram token;
- не принимает входящие запросы из интернета;
- имеет доступ только к payout secret;
- может быть остановлен большую часть месяца;
- запускается только для approved batch.

## Защита секретов

Разделить секреты на классы:

### Read-only secrets

- Telegram bot token.
- Affiliate/read API keys.
- DB password для bot app.

### High-risk secrets

- Withdrawal/transfer API key.
- DB password для payout executor.
- Master key для расшифровки wallet addresses.

High-risk secrets не должны лежать в обычном `.env` рядом с bot config.

Рекомендации:

- Использовать отдельный secret store: Vault, cloud KMS/Secret Manager или хотя
  бы Docker secrets/host files с правами `600`.
- Не передавать withdrawal key через environment variables, если можно смонтировать
  как secret file.
- Master key хранить отдельно от БД и backup-ов.
- Ротация ключей по расписанию и сразу после подозрения на утечку.
- IP whitelist на стороне биржи для всех API keys.
- Для withdrawal key включить address whitelist, chain whitelist и дневные лимиты,
  если биржа это поддерживает.
- Не выдавать withdrawal key trade/margin права.
- Если биржа не позволяет ограничить withdrawal key адресами/лимитами, лучше
  оставить выплаты ручными или полуавтоматическими.

## Защита БД

Данные в БД чувствительные:

- Telegram ID.
- UID на биржах.
- BEP20-адреса.
- суммы выплат.
- txid и история выплат.

Рекомендации:

- PostgreSQL, не SQLite, для payout-системы.
- Отдельные DB roles:
  - `bot_app`: заявки, расчёты, чтение безопасных представлений;
  - `payout_exec`: только approved batches/items;
  - `admin_readonly`: отчёты;
  - migration role отдельно.
- Минимальные GRANT privileges.
- Row-Level Security можно использовать как дополнительный default-deny слой,
  особенно если появится web-admin или несколько сервисов.
- Wallet address хранить encrypted-at-application-layer.
- Для поиска дублей хранить `address_hash = HMAC(secret_pepper, normalized_address)`.
- Полный адрес в audit/log не писать.
- Backups шифровать отдельно.
- Регулярно тестировать restore backup-а.
- Backup key хранить отдельно от backup files.
- Не хранить raw API responses целиком, если там есть лишние персональные данные.
  Лучше сохранять нормализованные records и request id.

## Exchange withdrawal/transfer API

Идеальная модель:

- Отдельный exchange account/subaccount для выплат.
- На нём лежит только сумма текущего batch плюс небольшой fee buffer.
- API key может только withdraw/transfer, без trade.
- IP whitelist на VPS.
- Address whitelist только на подтверждённые payout addresses, если биржа
  поддерживает.
- Chain hardcoded `BEP20`.
- Перед execution проверять:
  - chain;
  - address hash;
  - amount > 0;
  - amount >= minimum;
  - daily/monthly limits;
  - batch hash не изменился после approval;
  - хватает баланса.

Если выбранная биржа поддерживает только внутренний transfer, а не on-chain
withdrawal, надо отдельно описать payout destination. "Номер кошелька BEP20"
подразумевает именно on-chain withdrawal.

## Fraud/risk flags

Автоматически подсвечивать владельцу:

- один UID заявлен несколькими Telegram ID;
- один кошелёк используется несколькими Telegram ID;
- кошелёк добавлен или изменён прямо перед freeze;
- слишком крупная выплата относительно истории;
- новый Telegram user;
- UID впервые появился в системе;
- пользователь часто меняет кошельки;
- rebate amount резко отличается от предыдущего месяца;
- exchange adapter вернул warning или неполный период.

Risk flag не всегда блокирует выплату, но требует owner review.

## MVP

### Версия 1

- Приём заявок с 27 по последний день.
- Один активный BEP20-кошелёк на пользователя.
- Конфликты UID блокируют auto payout.
- Расчёт 1 числа.
- Сводка владельцу.
- Batch в статусе `ready_for_review`.
- Без автоматического withdrawal. Владелец платит вручную, бот помогает сверкой.

Это уже даст бизнес-процесс и накопит данные без риска автоматического слива.

### Версия 2

- Payout-executor.
- Отдельный withdrawal key на MEXC.
- Owner approval.
- Balance check.
- Execution с idempotency.
- Reconciliation по txid.

### Версия 3

- Web-admin panel.
- TOTP/WebAuthn для финального подтверждения.
- Two-person approval, если объёмы вырастут.
- Address whitelist automation, если MEXC API это позволяет.
- Более сильная аналитика risk flags.
- Конвертация non-USDT по manual rates или отдельный безопасный convert flow.

## Принятые бизнес-решения

- Payout asset: только `USDT_BEP20`.
- Payout-source: `MEXC`.
- Пользователю выплачивается вся USDT-сумма, которую возвращает API.
- Withdrawal fee оплачивает владелец.
- Минимальная автоматическая выплата: `10 USDT`.
- Суммы ниже минималки не выплачиваются автоматически.
- Один пользователь имеет один активный BEP20-кошелёк.
- Пользователь может менять кошелёк до freeze; поздние изменения подсвечиваются
  владельцу.
- Manual proof для первого claim UID пока не нужен.
- Второй approver пока не нужен.
- По raw exchange records: для MVP достаточно нормализованных расчётов,
  `raw_records_count`, request id и adapter notes. Полные raw responses лучше не
  хранить, чтобы не тащить в БД лишние персональные данные и потенциальные
  чувствительные поля.

## Открытые вопросы

- Проверить MEXC withdrawal API: BEP20 chain code, withdrawal minimum, precision,
  fee model, withdrawal status endpoint, address whitelist, IP whitelist, дневные
  лимиты.
- Нужно ли юридическое/налоговое описание процесса выплат пользователям в crypto?
