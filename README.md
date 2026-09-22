# Doner Club Kiosk

Отдельный проект киоска самообслуживания Doner Club.

## Локальный запуск (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app app run --host 127.0.0.1 --port 10000
```

Откройте http://127.0.0.1:10000. На Windows используется Flask, поскольку
Gunicorn предназначен для Unix. Ключи ниже задаются переменными окружения
до запуска; файл `.env` автоматически не загружается.
Без ключей интерфейс и `/health` доступны, а API меню возвращает JSON с HTTP 503.

## Проверки

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

Браузерный сценарий: `node tests/browser.cjs` при запущенном сервере,
установленных Microsoft Edge и пакете Playwright (`npm install --no-save --package-lock=false playwright`).
Опционально `KIOSK_URL` задаёт адрес сервера, `KIOSK_SCREENSHOT` — путь снимка.
Сценарий подменяет меню и конфигурацию тестовыми данными и не отправляет заказы.
Серверные тесты также подменяют обращения к iiko.

Обычный экран оплаты — демонстрация: банковская интеграция, отправка заказа
и скидка по телефону не реализованы. Реальные операции выполняет отдельный
экран «Тест в iiko» после двух подтверждений. Он записывает внешне оплаченную
продажу; запускайте его только при намерении создать такую продажу.
Успех API подтверждается состоянием команды `Success`; HTTP 202 означает,
что результат ещё не подтверждён. Перед повторной отправкой проверьте iiko,
поскольку запрос мог уже создать заказ.

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
