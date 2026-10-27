# OKX: точная комиссия по uid за произвольный период

**Дата:** 2026-07-10
**Статус:** утверждён

## Проблема

Текущий адаптер OKX ([bot/exchanges/okx.py](../../../bot/exchanges/okx.py)) ходит в
`GET /api/v5/affiliate/invitee/detail`, который отдаёт только **накопленную** (lifetime)
`totalCommission` без привязки к датам. Поэтому `supports_date_range = False` и бот
показывает справочное значение с дисклеймером.

Живая проверка (probes в scratchpad, сверка с кабинетной выгрузкой
`Commission details 2026-05-01 - 2026-06-29.csv`) показала: `GET /api/v5/affiliate/invitee/list`
с `periodType=custom` + `begin`/`end` + `keyword=uid` возвращает `totalCommission` **ровно за
период** — совпадает с CSV до 10 знаков на трёх uid и на произвольном окне 10.06–20.06.

## Решение

Полностью **заменить** эндпоинт на `/api/v5/affiliate/invitee/list` (режим `periodType=custom`).

### Поток `get_commission(uid, date_from, date_to)`

1. Нарезаем период на окна ≤90 дней через `iter_windows(date_from, date_to, 90)` (UTC-границы
   суток — VERIFIED: совпало с CSV на UTC). OKX-лимит: диапазон не длиннее 90 дней (иначе
   `code=499001`), поэтому нарезка обязательна для длинных периодов.
2. Для каждого окна: `begin`/`end` = Unix ms (`to_millis`), `periodType=custom`, `keyword=uid`,
   `limit=100`. Ищем в ответе строку с точным `uid` и суммируем её `totalCommission` (USDT).
3. Суммирование по окнам корректно — `totalCommission` аддитивна по непересекающимся
   интервалам (проверено сверкой с CSV).
4. **Гард на 180 дней:** если `date_from` старше ~180 дней от now — добавляем заметку, что
   данные глубже 180 дней OKX отдаёт молча как `0.0` (не ошибка!), значению доверять нельзя.
   Запрос всё равно выполняем.
5. Нет строки с uid ни в одном окне → пустой результат с заметкой «uid не найден / нет данных
   за период».
6. `supports_date_range = True`. Дисклеймер про «накопленную» убираем. Остаётся
   `settlement_note` про accrued ≠ payout.

### Подпись и HTTP

Схема подписи OKX v5 не меняется (`base64(hmac_sha256(ts+method+requestPath+body))`,
ISO-timestamp) — переиспользуется существующий `_headers` / `_iso_timestamp`. Постраничность:
`keyword=uid` сужает выдачу до одного реферала, поэтому одна страница на окно; строку
матчим по точному `uid`.

### Компромиссы / не входит в объём (YAGNI)

- Комиссия только в USDT (`totalCommission` уже в USDT) — как и раньше.
- `performance/summary` не используем (он агрегат по всем инвайти, не по одному uid).
- Разбивку по категориям (SPOT/DERIVATIVE/BSC) не тянем — нужна одна сумма.

## Тесты (httpx-mock, без живого API)

- uid найден в одном окне (≤90 дней) → сумма USDT из `totalCommission`; `periodType=custom`,
  `begin`/`end`/`keyword` в запросе; `supports_date_range is True`; нет заметки «накопленную».
- период >90 дней → несколько окон, суммирование; проверка числа запросов.
- uid отсутствует в ответе → пустой результат + заметка.
- `date_from` старше 180 дней → заметка-предупреждение.
