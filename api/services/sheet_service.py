from api.exceptions import ImportServiceError
from api.models import ImportBatchStatus
from api.services.mapping_service import MappingService
from api.services.parser_service import ParserService, VIRTUAL_HEADERS

class SheetService:
    GEMINI_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".eml", ".msg", ".txt"}

    def select_header(self, batch, sheet, header_row_number):
        if sheet.import_batch_id != batch.id:
            raise ImportServiceError("Sheet does not belong to this batch.")

        import os
        from pathlib import Path

        suffix = Path(batch.uploaded_file.original_file_name).suffix.lower()

        if suffix in self.GEMINI_EXTENSIONS or sheet.cached_rows:
           
            rows = sheet.cached_rows or [list(VIRTUAL_HEADERS)]
        else:
            rows = ParserService().parse(batch.uploaded_file.file, batch.uploaded_file.original_file_name)["sheets"][sheet.sheet_index]["rows"]

        if header_row_number < 1 or header_row_number > len(rows):
            raise ImportServiceError("Invalid header row.", {"header_row_number": ["Header row is outside the sheet range."]})
        headers = [str(cell or "") for cell in rows[header_row_number - 1]]
        samples = []
        for index, header in enumerate(headers):
            values = []
            for row in rows[header_row_number: header_row_number + 10]:
                values.append(row[index] if index < len(row) else None)
            samples.append({"uploaded_column_index": index, "uploaded_column_name": header, "sample_values": values[:5]})
        sheet.header_row_number = header_row_number
        sheet.data_start_row_number = header_row_number + 1
        sheet.total_rows = max(len(rows) - header_row_number, 0)
        sheet.cached_rows = rows
        sheet.cached_headers = headers
        sheet.save(update_fields=["header_row_number", "data_start_row_number", "total_rows", "cached_rows", "cached_headers", "updated_at"])
        batch.status = ImportBatchStatus.MAPPING_PENDING
        batch.total_rows = sheet.total_rows
        batch.save(update_fields=["status", "total_rows", "updated_at"])
        suggestions = MappingService().suggest(headers)
        return {
            "sheet_id": sheet.id,
            "header_row_number": header_row_number,
            "columns": samples,
            "sample_rows": rows[header_row_number: header_row_number + 10],
            "auto_mappings": suggestions,
            "document_metadata": batch.document_metadata,
            "extraction_summary": batch.extraction_summary,
            "document_field_meta": batch.extraction_summary.get("document_field_meta", {}),
        }
