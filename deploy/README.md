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
