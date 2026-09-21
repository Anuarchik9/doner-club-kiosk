# Doner Club Kiosk

Отдельный проект киоска самообслуживания Doner Club.

## Что находится здесь

- `static/kiosk-preview.html` — интерфейс киоска.
- `app.py` — отдельный Flask backend киоска.
- `/kiosk-menu` — внешнее меню iiko `Kiosk Арай`.
- `/kiosk-iiko-config` — терминальная группа и столы iiko.
- `/kiosk-test-paid-order` — временный тест оплаченного заказа.
- `/health` — health check для Render.

## Render

Build command:

```
pip install -r requirements.txt
```

Start command:

```
gunicorn app:app
```

## Environment variables

Секреты не хранить в GitHub.

Required:

- `IIKO_KIOSK_API_KEY`
- `IIKO_KIOSK_APP_ID` или `IIKO_APP_ID`
- `IIKO_KIOSK_CLIENT_SECRET` или `IIKO_CLIENT_SECRET`

Для чтения меню текущая конфигурация может также использовать `IIKO_API_KEY`.
Опционально: `IIKO_KIOSK_PAYMENT_TYPE_ID`.

Будущий домен: `kiosk.donerclub.kz`.
