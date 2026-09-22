# Checkout prompt log

TartanConnect's order export does not include the question buyers saw, only what
they typed (the `Comments` column). This log is the record of the exact prompt
shown at checkout, so every stored consent can be matched to the disclosure the
buyer read. Add a new version here, with its effective time in UTC, every time the
"Enter custom questions" text changes on any listing, and keep every listing
byte-identical. The importer only grants call/text consent for purchases made at or
after `CONSENT_PROMPT_EFFECTIVE_FROM` (the effective time of the first version
below that carries the disclosure).

Raw exports are archived unchanged in the `export_uploads` table on every commit.
Keep consent and revocation records for at least 4 years after the last call or
text to a number.

## Version 1 — effective 2026-09-22T18:55:00Z

Applied to all six shirt listings (`ScottyLabs Found T-Shirt, Size XS/S/M/L/XL/XXL
(Pittsburgh Pickup Only, No Refunds)`) and the officers-only test item. Verified
rendering in full at checkout on 2026-09-22.

```text
REQUIRED: type YES, your CMU email (@andrew.cmu.edu or @cmu.edu), your own mobile number (non-US: + and country code). Example (use your own): YES, andrewid@andrew.cmu.edu, 412-555-0123. YES means you agree to: pickup only, at Tepper 3808 on CMU's Pittsburgh campus, Saturdays 4 to 5 PM ET (changes are emailed); no shipping; no refunds; and emails from ScottyLabs about this order. OPTIONAL AI-VOICE CALLS AND TEXTS: add CALLS OK at the end, only if the number is yours. ScottyLabs may then call you with an automated, AI-generated voice and text you pickup reminders for this order. No marketing. Not required to buy. Message frequency varies. Msg & data rates may apply. Opt out anytime, any way: reply STOP to a text, say stop on a call, or email scottylabs@cmu.edu. Don't want them? Leave it out. Buying 2+ sizes? Same line in each box; one code covers the order. Questions or no CMU email? Email scottylabs@cmu.edu, not this box.
```

## Version 0 — before 2026-09-22T18:55:00Z (no call/text disclosure)

```text
Type YES to confirm: I understand this shirt is PICKUP ONLY at a weekly ScottyLabs GBM (no shipping), and I (or someone I send) must show my emailed pickup code to collect it.
```
