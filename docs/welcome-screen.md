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
The background is the user's supplied 1600 × 1200 photograph `обложка Яндекс.jpg`,
encoded as `static/welcome-photo.webp` without changing its composition. CSS cover
adapts it to the screen, with a dark overlay to keep white text legible.

The welcome screen uses self-hosted Noto Sans (variable weight 100–900), with
Latin and Cyrillic glyphs including all Kazakh letters. This prevents mixed-font
fallback for Ә, Ғ, Қ, Ң, Ө, Ұ, Ү, Һ and І. The font is from Google Fonts' Noto Sans
distribution; its SIL Open Font License is included in `static/fonts/OFL.txt`.
Font changes are scoped to the welcome screen.

## Validation

Run `python -m unittest discover -s tests -q` and `node tests/frontend.cjs`.
The offline frontend checks cover all three languages, mode gating, resetting for
the next guest, translating a menu failure received before language selection,
cart prices, service modifiers, unavailable products and expired availability.
The optional Playwright suite in `tests/browser.cjs` also includes the new entry flow.

Manual local browser checks use fixture GET responses and never send orders or
payments. The payment screen is still a demonstration, not connected Kaspi payment.
