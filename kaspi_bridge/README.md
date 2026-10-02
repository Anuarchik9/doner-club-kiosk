# Doner Club Kaspi Bridge — home test

This folder is a **test-only** local bridge for Kaspi Smart POS. Payment methods are intentionally not implemented yet.

## What it tests

1. The Windows PC can reach Smart POS on TCP port 8080.
2. The cash register can be registered with Smart POS via `GET /v2/register`.
3. The Smart POS grants and returns access/refresh tokens.
4. The token can access `GET /v2/deviceinfo`.
5. The refresh token can be renewed via `GET /v2/revoke`.

Tokens are stored locally in:

`%LOCALAPPDATA%\DonerClubKaspiBridge\tokens.json`

Do not upload that token file to GitHub or send it in chat.

## Before running at home

The Smart POS and Windows PC **must be connected to the same private local network**.

After moving Smart POS from Republic to the home network, open **О терминале → Информация** and check **IP терминала** again. It may no longer be `192.168.1.8`.

If the IP changed, edit `.env` and set:

`SMART_POS_HOST=<new IP>`

## Run

Double-click:

`install_and_run.bat`

Then open:

`http://127.0.0.1:8765`

Use the buttons in order:

1. **Проверить порт 8080**
2. **Зарегистрировать кассу**
3. On Smart POS, approve the API access request
4. **Проверить Smart POS**

A successful device check should return Smart POS device data.

## Important

- The official Kaspi Smart POS API uses HTTPS on port 8080.
- Registration does not require an access token.
- After registration, `accessToken` is sent in the HTTP header `accesstoken`.
- Kaspi's access token expires and is renewed using the refresh token.
- The test bridge binds to `127.0.0.1`, so it is not exposed to the LAN or internet.
- There is deliberately **no payment endpoint** in this build. Real payment will be added only after connectivity and registration are confirmed.
