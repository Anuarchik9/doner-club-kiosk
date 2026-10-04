# Self-service order contract

Kiosk and CALL CENTRE share the iiko order gateway in `app.py`. The public
website currently has PayLink disabled and does not create iiko orders directly.
Future website checkout must use the same contract.

- Do not fetch/select tables, including legacy `IIKO_KIOSK_*_TABLE_ID` values.
- `/api/1/order/create` supports nullable/omitted `tableIds` in iiko's official
  `TableOrder` schema. Use the point's validated terminal group only. Do not
  fall back to a table or to another point if iiko rejects the configuration.
- `tabName` is explicitly empty. Do not send technical channel names as kitchen
  comments. Channel attribution remains in CRM and the deterministic external ID.
- Unpaid tests omit `payments` and never call `/order/close`. Cashiers choose the
  real cash/card/QR payment. Never simulate received money for a layout test.
- Genuine paid orders retain their payment and close/fiscalization flow. Do not
  remove an actual payment, relabel it as cash, or change other orders or register
  settings. iikoFront's remembered UI selection is not exposed by this API; an
  on-register check is still required to verify that behavior.
- Kiosk requires an existing Card payment ID in
  `IIKO_KIOSK_<ARAI|RESPUBLIKA>_PAYMENT_TYPE_ID` (or the legacy shared
  `IIKO_KIOSK_PAYMENT_TYPE_ID`). Technical Kiosk/Analytics names are rejected.
  Readiness validates the mapping before Smart POS charging. Do not enable live
  payments without checking the mapping with the restaurant's accounting setup.
- CALL CENTRE retains the explicit existing remote-payment mapping. It cannot
  use a Kiosk/Analytics override either. No payment dictionaries are created or
  changed by the gateway.

Official schema: https://api-ru.iiko.services/api-docs/docs

Run `python -m unittest discover -s tests -q`. Tests mock iiko: no production
orders, payments, kitchen printing or cashier actions are generated.
