import csv
import io
import mimetypes
from pathlib import Path


class DocumentPreprocessor:
    """Build lightweight deterministic representations without consuming the file."""

    def prepare(self, file_field, original_file_name):
        name = str(original_file_name or "")
        suffix = Path(name).suffix.lower()
        self._rewind(file_field)
        raw = file_field.read()
        self._rewind(file_field)
        if suffix == ".pdf":
            return self._pdf(raw)
        if suffix in {".xlsx", ".xls"}:
            return self._spreadsheet(raw, suffix, name)
        if suffix == ".csv":
            return self._csv(raw, name)
        if suffix == ".msg":
            return {"file_type": "msg", "text": "", "has_usable_text": False,
                    "original_file_name": name}
        return {"file_type": "image" if mimetypes.guess_type(name)[0] and
                mimetypes.guess_type(name)[0].startswith("image/") else "other",
                "text": "", "has_usable_text": False, "original_file_name": name}

    @staticmethod
    def _rewind(file_field):
        try:
            file_field.seek(0)
        except (AttributeError, ValueError):
            try:
                file_field.open("rb")
            except AttributeError:
                pass

    def _pdf(self, raw):
        text, pages = "", 0
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                pages = len(pdf.pages)
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception:
            pass
        return {"file_type": "pdf", "text": text, "has_usable_text": bool(text.strip()),
                "page_count": pages, "original_bytes": raw}

    def _spreadsheet(self, raw, suffix, name):
        rows, text = [], []
        if suffix == ".xlsx":
            import openpyxl
            book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            sheets = ((sheet.title, sheet.iter_rows()) for sheet in book.worksheets)
        else:
            import xlrd
            book = xlrd.open_workbook(file_contents=raw, on_demand=True)
            sheets = (
                (
                    sheet.name,
                    (sheet.row_values(row_number) for row_number in range(sheet.nrows)),
                )
                for sheet in book.sheets()
            )
        for sheet_name, sheet_rows in sheets:
            for row_number, values in enumerate(sheet_rows, 1):
                if suffix == ".xlsx":
                    number_formats = [str(cell.number_format or "") for cell in values]
                    values = [cell.value for cell in values]
                else:
                    number_formats = []
                # Formula results such as 30 * 3.09 are stored as binary
                # floats (92.69999999999999); keep the value Excel displays.
                values = ["" if value is None else str(round(value, 10) if isinstance(value, float) else value).strip()
                          for value in values]
                # Some workbooks carry formatting across thousands of empty
                # columns. Keeping those cells makes every later row/column
                # pass unnecessarily large and can cause the tail of a valid
                # table to be missed or timed out. Preserve meaningful cells
                # and their formats, but discard only trailing blanks.
                last_value = next((index for index in range(len(values) - 1, -1, -1)
                                   if values[index]), -1)
                values = values[:last_value + 1]
                if number_formats:
                    number_formats = number_formats[:last_value + 1]
                rows.append({"sheet": sheet_name, "row_number": row_number, "values": values, "number_formats": number_formats})
                text.append(" | ".join(values))
        return {"file_type": "spreadsheet", "rows": rows, "text": "\n".join(text),
                "has_usable_text": bool(text), "original_file_name": name}

    def _csv(self, raw, name):
        decoded = raw.decode("utf-8-sig", errors="replace")
        rows, text = [], []
        for row_number, values in enumerate(csv.reader(io.StringIO(decoded)), 1):
            values = [value.strip() for value in values]
            rows.append({"sheet": "CSV", "row_number": row_number, "values": values})
            text.append(" | ".join(values))
        return {"file_type": "spreadsheet", "rows": rows, "text": "\n".join(text),
                "has_usable_text": bool(text), "original_file_name": name}
