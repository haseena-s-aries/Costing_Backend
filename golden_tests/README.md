# Golden Test Harness — Gemini Extraction Pipeline

## Why this exists

Every time a new document format broke extraction, the old workflow was:
edit the prompt or add a regex fix → hope it works → move on. There was no
way to know if that fix broke a *previously working* format. This harness
closes that loop.

**Rule going forward: no prompt or extraction-logic change ships without
running this and confirming the pass rate didn't drop.**

## Folder structure

```
golden_tests/
  inputs/       <- real source files (pdf, xlsx, msg, images)
  expected/     <- hand-verified correct JSON output, same basename as input
  results/      <- auto-generated per run, timestamped, gitignore this
  scorer.py     <- comparison engine
  run_golden_tests.py  <- runner
```

## Setup (one time)

1. Copy `scorer.py` and `run_golden_tests.py` into your Django project
   (e.g. `management/commands/` folder or a standalone `tools/` dir).
2. Fix the import line in `run_golden_tests.py`:
   ```python
   from api.services.gemini_extraction_service import GeminiExtractionService
   ```
   to match your actual module path (based on the pasted file, this is
   likely already correct — adjust `config.settings` to your real
   settings module too).
3. Make sure `GEMINI_API_KEY` is set in your environment.

## Building your golden set (do this first)

Pick 15-20 files that represent every shape you've had to patch for:
- a normal single-supplier invoice
- a multi-page quotation table
- an MSG email with an attached PDF quote
- an MSG email with inline priced text (no attachment)
- a spreadsheet with multiple supplier price columns
- a spreadsheet with FINAL/REVISED price stages
- a transport/freight tariff email
- a scanned/handwritten invoice
- a bundle of multiple unrelated invoices in one PDF
- a parallel-currency (RMB+USD) row document
- ...and any other case you've had to write a patch for

For each:

```bash
python run_golden_tests.py --dump-only --only invoice_001.pdf
```

This runs extraction and saves the raw output to
`results/<timestamp>/invoice_001.actual.json`. Open that file, manually
correct every wrong value, remove hallucinated rows, add missed rows — this
corrected version becomes `expected/invoice_001.json`. This is the ONE
manual, tedious step. Do it once per file, carefully — this is your source
of truth from now on.

## Running the test suite

```bash
python run_golden_tests.py
```

This extracts every file in `inputs/`, compares against `expected/`, and
prints a pass/fail + score per file, plus a rollup summary:

```
=== SUMMARY ===
{
  "total_files": 18,
  "passed": 14,
  "pass_rate": 0.7778,
  "average_score": 0.891,
  "worst_5_files": [...]
}
```

Full per-file reports (`*.report.json`) show exactly what went wrong:
`missing_items` (rows that should've been extracted but weren't),
`extra_items` (hallucinated/duplicate rows), and `item_field_mismatches`
(row was found but a field like unit_price or product_name is wrong).

## Workflow for every future prompt/code change

1. `python run_golden_tests.py` — note current pass_rate/average_score.
2. Make your change (prompt edit, new regex, whatever).
3. `python run_golden_tests.py` again.
4. If pass_rate or average_score DROPPED — you fixed one thing and broke
   another. Look at the report diff to see which file regressed, and
   either adjust your change to not affect that shape, or route that
   shape through a different prompt path (see main recommendation about
   classify-then-route).
5. Only commit/deploy if the new run is >= the old run.
6. Add the file that originally caused this change as a NEW golden file
   if it isn't already one, so this exact bug can never silently
   reappear.

## Adding a new format later

When a genuinely new document shape shows up in production:
1. Save the real file into `inputs/`.
2. `--dump-only` it, hand-correct into `expected/`.
3. NOW you can safely iterate on the prompt/code, checking full suite
   after every change.

This is the only way the fix-cycle actually terminates instead of
regenerating itself forever.
# Golden extraction regression workflow

Golden tests are a regression gate, not an automatic prompt-tuning system.
The expected JSON is the reviewed ground truth and must never be replaced by
the latest actual output just to make a test pass.

## Recommended workflow

1. Run the complete set:

   `python golden_tests/run_golden_tests.py`

2. Read `results/<run>/_summary.json`. Group failures using
   `failure_category_counts` instead of fixing files one by one.

3. Fix the appropriate layer:

   - `missing_items` / `no_items_extracted`: page splitting, spreadsheet
     chunking, or a bounded recovery extraction.
   - `extra_items`: subtotal/tax/duplicate filtering and row validation.
   - `wrong_item_fields`: normalization or field binding; only then consider
     a prompt change.
   - `wrong_document_header`: header/source-section selection.

4. Add the failing document to a small fixture group for that failure type,
   change one thing, and rerun that group with `--only`.

5. Run the complete set before release. The command exits with code 2 when
   any golden case fails, so CI or a deployment script can stop safely.

Use `--no-fail` only when exploring or creating a new fixture. Use
`--dump-only` only to create a draft; manually review and correct the draft
before copying it to `expected/`.

## Production policy

Do not call Gemini repeatedly until a result matches expected JSON. Production
extraction should make one normal request, at most one targeted recovery for
missing pages/rows, then validate the result and mark it for review. Golden
tests improve the pipeline offline; they should not increase per-file token
usage in production.
