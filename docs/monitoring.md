# Наблюдение за сервисом

## Что есть
| Что | Где | Как смотреть |
|---|---|---|
| Жив ли бот, служба и диск | `deploy/healthcheck.sh`, таймер раз в 2 минуты | сообщение в Telegram при падении и при восстановлении (нужен `ALERT_CHAT_ID`) |
| Числа работы бота | страница `/metrics` | Prometheus или `curl`, нужен `METRICS_TOKEN` |
| Те же числа строкой | журнал `journalctl -u defenceai`, строка `metrics ...` раз в 5 минут, если что-то изменилось | `journalctl -u defenceai \| grep metrics` |
| Резервные копии | `deploy/backup.sh` раз в сутки и `deploy/verify-backup.sh` раз в неделю | сообщение при проблеме, раз в месяц сообщение «всё хорошо» |

## Метрики
Страница закрыта, пока в `.env` не задан `METRICS_TOKEN`. Дальше доступ только с заголовком `Authorization: Bearer ТОКЕН`.
```bash
curl -H "Authorization: Bearer $TOKEN" https://ТВОЙ_ДОМЕН/metrics
```
| Метрика | Что показывает |
|---|---|
| `defenceai_comments_total{action}` | сколько комментариев обработано и что с ними сделано |
| `defenceai_comment_seconds` | время обработки одного комментария |
| `defenceai_jev_requests_total{outcome}` | запросы к ИИ: `ok`, `error`, `busy` (очередь переполнена), `breaker_open` (ИИ временно отключён после сбоев) |
| `defenceai_jev_seconds`, `defenceai_jev_wait_seconds` | ответ ИИ и ожидание свободного места в очереди |
| `defenceai_jev_queue`, `defenceai_jev_running` | сколько запросов ждёт и сколько выполняется |
| `defenceai_telegram_errors_total{action}` | отказы Telegram (обычно нет прав у бота) |
| `defenceai_telegram_flood_waits_total` | сколько раз Telegram просил «помедленнее» |
| `defenceai_payments_total{currency}`, `defenceai_payment_amount_total{currency}` | платежи в рублях и звёздах |
| `defenceai_chats{state}` | чатов на пробе, платящих и с закончившимся сроком |
| `defenceai_pending_reviews` | сколько сомнительных комментариев ждёт решения владельцев |
| `defenceai_db_bytes`, `defenceai_uptime_seconds` | размер базы и время работы процесса |

Счётчики сбрасываются при перезапуске бота, это нормально для Prometheus (он считает прирост).

## Подключение Prometheus
```yaml
scrape_configs:
  - job_name: defenceai
    scheme: https
    metrics_path: /metrics
    authorization: {credentials: ТОКЕН}
    static_configs: [{targets: ["ТВОЙ_ДОМЕН"]}]
```

## Что считать тревогой
| Условие | Что это значит |
|---|---|
| `rate(defenceai_jev_requests_total{outcome="busy"}[5m]) > 0` | очередь к ИИ переполняется: рейд или слишком медленный ответ ИИ |
| `defenceai_jev_queue > 50` дольше минуты | то же, раньше срабатывает |
| `rate(defenceai_jev_requests_total{outcome=~"error\|breaker_open"}[5m]) > 0.1` | ИИ недоступен или отвечает ошибками |
| `histogram_quantile(0.95, rate(defenceai_comment_seconds_bucket[5m])) > 5` | комментарии обрабатываются дольше 5 секунд |
| `rate(defenceai_telegram_errors_total[10m]) > 0` | у бота пропали права в каком-то чате |
| `defenceai_chats{state="expired"}` растёт | клиенты не продлевают тариф |
| `defenceai_db_bytes` растёт быстрее недели подряд | не работает очистка журнала |

Метрики не заменяют проверку живости: если процесс упал, страница тоже недоступна, и об этом скажет `healthcheck.sh`.

## Очередь к ИИ
Запросы к ИИ идут через общую очередь на 20 мест. Чаты ждут по кругу, поэтому рейд в одном чате не задерживает остальные: тихий чат ждёт не дольше одного круга. Если в очереди одного чата накопилось 200 запросов или запрос ждёт больше 30 секунд, он не отправляется в ИИ, а проверяется локальным фильтром (очевидный спам со ссылками), владельцу при этом тревожного сообщения не приходит.

## Проверка резервной копии
`verify-backup.sh` раз в неделю (воскресенье 04:30) берёт свежую копию, распаковывает во временную папку, запускает проверку целостности SQLite, открывает её кодом самого бота (так проверяется и обновление схемы) и сравнивает с рабочей базой. Если копии нет, она старше 36 часов, повреждена, пуста или в ней больше платежей, чем в рабочей базе (ловушка при восстановлении), приходит сообщение. Вручную:
```bash
cd /opt/defenceai && .venv/bin/python -m scripts.verify_backup --dest /var/backups/defenceai --live bot.db
```
