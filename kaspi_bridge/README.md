# Doner Club Republic Kiosk Bridge

The bridge connects the Republic kiosk to **Kaspi Smart POS** on the local network and serves the current Doner Club kiosk UI to the iPad.

## On-site network

The following devices must be on the same Republic LAN/Wi-Fi:

- Windows PC running this bridge
- Kaspi Smart POS
- kiosk iPad

Smart POS is reached locally on port **8080**. The bridge listens on port **8765**.

## First launch at Republic

1. Connect the PC and Smart POS to the Republic network.
2. On Smart POS, check its current local IP.
3. Put that IP in `.env` as `SMART_POS_HOST`.
4. Run `install_and_run.bat`.
5. If Windows Firewall asks, allow Python for **Private networks**.
6. On the PC open `http://127.0.0.1:8765`.
7. Press **Проверить порт 8080** and **Проверить Smart POS**.
8. Register the cash register only if the saved token is missing/invalid.
9. The bridge page shows the PC local IP. On the iPad open:
   `http://<PC-IP>:8765/respublica`

Keep the bridge window open while the kiosk is operating.

## What the iPad route does

The local `/respublica` route loads the current kiosk from `kiosk.donerclub.kz`, but payment calls stay on the Republic PC. This avoids the iPad trying to reach `127.0.0.1` and keeps Smart POS inside the local network.

Flow after activation:

**Kiosk → Smart POS → successful payment → iiko Republic → existing iiko fiscalization/KDS**

The same Kaspi `processId` is used to create a deterministic iiko order ID. Retrying order submission after a successful payment does not create a second paid order.

## Before opening to guests

Do one controlled end-to-end transaction and confirm:

- amount appears correctly on Smart POS;
- payment succeeds once;
- order appears in iiko Republic;
- iiko order total equals the paid amount;
- order closes with payment type **Kiosk**;
- fiscal receipt is produced by the existing iiko/KKM setup;
- kitchen/KDS receives the order;
- stop-list blocks unavailable items.

Live payment activation is intentionally controlled by the server flag `KIOSK_LIVE_PAYMENTS_ENABLED`. Keep it disabled until the on-site checks above are ready.
