# Extraction regression testing

The production flow is:

`BulkUploadView → BulkImportService → bulk_import_worker → ImportService.preview/start → ExtractionOrchestrator → deterministic parser or Gemini → normalize/reconcile → gemini_result_to_virtual_sheet → ImportRow`.

Regression inputs belong in `test_fixtures/inputs/`. Expected extraction files belong in `test_fixtures/expected/extraction/`; expected import files belong in `test_fixtures/expected/import/`. Results and reports are written under `test_fixtures/results/` and `test_fixtures/reports/`.

Commands (run from the Django project directory):

```powershell
python manage.py create_golden_fixture sample.pdf
python manage.py run_extraction_regression --file sample.pdf
python manage.py run_import_regression --file sample.pdf
python manage.py run_extraction_batch --input-dir E:\data\quotes --output-dir test_fixtures\results\full_batch --recursive
python manage.py test api
```

Draft fixtures are written as `*.draft.json` and are never treated as approved expected output. Review them manually and rename/copy them to the matching `.json` path only after verification.

The comparator checks business document/item fields, uses numeric normalization for prices and quantities, matches items by product identity and price rather than array position, and reports missing, unexpected, and mismatched fields. Internal IDs, timestamps, token usage, and private debug keys are not required.

The extraction command uses `ExtractionOrchestrator`, the same service used by production imports. The import command uses `ImportService.preview()` and `ImportService.start()` inside a rollback-only transaction and deletes the temporary uploaded file, so regression rows are not persisted to the production database.

For a new bug: add a deterministic unit test first when possible, add or correct a golden fixture for file-level behavior, confirm the fixture fails before the generic fix, run the focused test, then the critical golden set, and periodically run the full batch. Gemini calls cost tokens; default execution is one selected file and batch workers are intentionally single-threaded.
