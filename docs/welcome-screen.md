# Welcome screen

Every page load starts with Kazakh, Russian and English ordering buttons. The chosen
language opens the existing dine-in/takeaway screen; returning to languages keeps
the cart. Completing the payment demonstration clears the cart and customer phone
and returns to language selection for the next guest.

English covers the interface, category labels, availability, cart, phone, payment
demonstration and iiko test warnings. Product names, descriptions and modifiers
remain the original iiko catalogue text, as they did in the Kazakh interface.

The new screen is styled in `static/welcome.css`. Existing menu/product/cart styles
are retained; the small-screen header may wrap to accommodate the third language.
The 1024 × 1536 background is served locally as `static/welcome-doner.webp` (about
170 KB), with a dark fallback colour and no external image dependency.

## Background provenance

Generated with the built-in imagegen tool, then encoded as WebP for delivery.
Final prompt:

> Premium photorealistic grilled doner wrap halves on a dark charcoal background with warm orange light. Portrait 2:3, food centred with quiet upper space for the brand and dark lower space for language buttons. No text, logos, watermarks or UI.

## Validation

Run `python -m unittest discover -s tests -q` and `node tests/frontend.cjs`.
The offline frontend checks cover all three languages, mode gating, resetting for
the next guest, translating a menu failure received before language selection,
cart prices, service modifiers, unavailable products and expired availability.
The optional Playwright suite in `tests/browser.cjs` also includes the new entry flow.

Manual local browser checks use fixture GET responses and never send orders or
payments. The payment screen is still a demonstration, not connected Kaspi payment.
