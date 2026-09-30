# Развёртывание на сервере (Ubuntu 22.04+)

Все команды выполняются на твоём компьютере и на сервере, не в чате.

## 0. Проверь, что сервер видит Telegram и Jev

```bash
ssh -i servak.pem root@IP_СЕРВЕРА
curl -sS -o /dev/null -w "telegram: %{http_code}\n" https://api.telegram.org
curl -sS -o /dev/null -w "jev: %{http_code}\n" https://api.typesafe.ai/v1/models
```

Оба должны вернуть код ответа (у Jev будет 401: ключ мы не передали). Если завис — сервер не годится.

## 1. Скопируй проект на сервер

С твоего компьютера, из папки с репозиторием:

```bash
scp -i servak.pem -r . root@IP_СЕРВЕРА:/root/defenceai
```

или на сервере: `git clone -b claude/trusting-thompson-cr3ng7 https://github.com/dmitriii501/telegabotantispam /root/defenceai`

## 2. Установи

```bash
cd /root/defenceai
bash deploy/install.sh
nano /opt/defenceai/.env      # вписать TELEGRAM_BOT_TOKEN и TYPESAFE_API_KEY
systemctl restart defenceai
```

## 3. Проверь

```bash
systemctl status defenceai
journalctl -u defenceai -f    # живые логи, выход Ctrl+C
```

В логах должна появиться строка `Run polling for bot @DefenceAiBot`.

## Обновление

Снова скопировать проект и выполнить `bash deploy/install.sh`: `.env` и база `bot.db` не затрагиваются.

## Веб-панель (Mini App)

Нужен домен, который указывает на IP сервера (подойдёт бесплатный поддомен DuckDNS), и открытые порты 80 и 443.

```bash
cd /root/defenceai && git pull
bash deploy/install.sh
bash deploy/setup_https.sh defenceaibot.duckdns.org
```

Скрипт ставит Caddy (он сам получает и продлевает сертификат), прописывает `WEBAPP_URL` в `/opt/defenceai/.env`
и перезапускает бота. После этого:

- в личке с ботом появится кнопка «Панель» рядом с полем ввода;
- команда `/panel` в группе присылает админу ссылку на панель именно этого чата.

Если сертификат не выдаётся: проверь `journalctl -u caddy -f`, что домен указывает на IP сервера
(`getent hosts defenceaibot.duckdns.org`) и что порты 80 и 443 открыты в панели хостера.

## Проверка живости и алерт

`deploy/install.sh` включает таймер `defenceai-health.timer`: раз в 2 минуты `healthcheck.sh` проверяет службу, адрес `/health` и место на диске.
Чтобы получать сообщения в Telegram, впиши в `/opt/defenceai/.env` свой номер (`ALERT_CHAT_ID=...`, узнать у @userinfobot), напиши боту `/start` и выполни `systemctl restart defenceai`.
Сообщение приходит один раз при падении и один раз при восстановлении. Проверить вручную: `bash /opt/defenceai/deploy/healthcheck.sh`.

## Резервные копии

`deploy/install.sh` включает таймер: каждый день в 03:30 база копируется в `/var/backups/defenceai/`
(хранится 14 дней, файлы `bot-ГГГГ-ММ-ДД.db.gz`). Проверить: `systemctl list-timers defenceai-backup.timer`,
запустить вручную: `systemctl start defenceai-backup.service`.

Восстановление:

```bash
systemctl stop defenceai
gunzip -c /var/backups/defenceai/bot-ГГГГ-ММ-ДД.db.gz > /opt/defenceai/bot.db
chown defenceai:defenceai /opt/defenceai/bot.db
systemctl start defenceai
```

Копии лежат на том же сервере, поэтому от потери самого сервера они не защищают:
время от времени скачивай свежий файл к себе (`scp -i servak.pem root@IP:/var/backups/defenceai/bot-*.db.gz .`).
