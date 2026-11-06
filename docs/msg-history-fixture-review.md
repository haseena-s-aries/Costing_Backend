# MSG history fixture review — 2026-09-15

Reviewed original Outlook bodies and MSG sent-date metadata locally, not just
Gemini output. Filenames are fixture selectors only, never extraction rules.

- Battery: expected stays unchanged. Supplier offer on 12 August states
  `Total Cost: AED 58,150/-`. The 17 August reply revises it to AED 43,300 for
  maximum 20 days and quotes AED 2,150 per additional day. Neither duration
  nor an unspecified future day implies quantity 1. There is no thread-wide
  grand total combining these alternatives.
- NGFS: both dated cargo tables really contain separate `DIMENSION IN CM`
  and `COMMODITY` fields. Added them to expected attributes/display text.
  Historical dimensions are `61X45X117 CM PER EACH`; latest dimensions are
  `TBA`; commodity is `GENERAL CARGO` in each table. The original prices use
  comma decimals with dot thousands. Both prices, dates and row counts remain.
- Rashed: the 18 August reply gives a numbered item and `Dhs 16800.00 + VAT 5 %`
  but no quantity/UOM or extended amount. Changed only that row's quantity,
  UOM and line total to null. The March and July offers explicitly state
  `QTY - 1 No` and remain unchanged. `1)` is a row number, not a quantity.
  The July buyer request copies 15750; it is not a fourth supplier quote.

The expected comparator is unchanged. Tests additionally verify source block
ownership, exact row dates/prices, attributes and repeated normalization.
Offline replay reports are labelled as such; they are not live Gemini runs.
