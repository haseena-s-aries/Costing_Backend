import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse
from django.utils import timezone
from api.models import PurchaseQuoteDownload
import requests
from bs4 import BeautifulSoup

from django.conf import settings
from django.core.management.base import BaseCommand

from api.constants import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

LOGIN_PAGE_URL = "https://purchaseapproval.markndget.com"
LOGIN_POST_URL = "https://purchaseapproval.markndget.com/dologin"
LIST_URL = "https://purchaseapproval.markndget.com/download-quotes"
PAGE_URL = "https://purchaseapproval.markndget.com/download-quotes?page={page}"
DOWNLOAD_URL = "https://markndget.com/testfile/download.php?file={file_name}"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class Command(BaseCommand):
    help = "Download quotation files from Purchase SW website."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--list-only", action="store_true")
        parser.add_argument("--debug-html", action="store_true")
        parser.add_argument(
            "--new-only",
            action="store_true",
            help="Download only files newer than last successful sync.",
        )

    def handle(self, *args, **options):
        download_dir = Path(settings.PURCHASE_QUOTES_DOWNLOAD_DIR)
        logger.info("Purchase quote sync started")
        download_dir.mkdir(parents=True, exist_ok=True)

        state_path = download_dir / "sync_state.json"
        last_synced_at = self._load_last_synced_at(state_path)

        if options["new_only"]:
            if last_synced_at:
                self.stdout.write(
                    f"New-only mode. Last synced at: {last_synced_at.strftime(DATE_FORMAT)}"
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "New-only mode requested, but no sync_state.json found. Full scan will run."
                    )
                )

        session = requests.Session()
        self._login(session)

        rows = self._get_all_file_rows(
            session=session,
            debug_html=options["debug_html"],
            last_synced_at=last_synced_at if options["new_only"] else None,
        )

        self.stdout.write(f"Found {len(rows)} unique file row(s).")

        if options["list_only"]:
            for index, row in enumerate(rows, start=1):
                self.stdout.write(
                    f"{index}. page={row['page']} row={row['row']} "
                    f"date={row['quote_date_text'] or '-'} file={row['file_name']}"
                )
            return

        downloaded = 0
        skipped = 0
        failed = 0
        limit = options["limit"]
        new_files = []
        newest_downloaded_at = None

        for row in rows:
            file_name = row["file_name"]
            quote_date = row["quote_date"]

            if quote_date and (
                newest_downloaded_at is None or quote_date > newest_downloaded_at
            ):
                newest_downloaded_at = quote_date

            suffix = Path(file_name).suffix.lower()

            if suffix not in SUPPORTED_EXTENSIONS:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipped unsupported file: page={row['page']} row={row['row']} file={file_name}"
                    )
                )
                logger.warning(
                    "Skipped unsupported purchase quote file file=%s extension=%s page=%s row=%s",
                    file_name,
                    suffix,
                    row["page"],
                    row["row"],
                )
                skipped += 1
                continue

            local_path = download_dir / file_name

            if local_path.exists() and local_path.stat().st_size > 0:
                self.stdout.write(
                    f"Skipped already downloaded: page={row['page']} row={row['row']} file={file_name}"
                )
                logger.info(
                    "Skipped already downloaded purchase quote file file=%s page=%s row=%s",
                    file_name,
                    row["page"],
                    row["row"],
                )
                skipped += 1

                continue

            if limit and downloaded >= limit:
                self.stdout.write(
                    self.style.WARNING(
                        f"Limit reached. Downloaded {downloaded} new file(s)."
                    )
                )
                break

            url = DOWNLOAD_URL.format(file_name=quote(file_name))

            self.stdout.write(
                f"Downloading: page={row['page']} row={row['row']} "
                f"date={row['quote_date_text'] or '-'} file={file_name}"
            )

            try:
                file_response = session.get(url, timeout=120)
                file_response.raise_for_status()

                content_type = file_response.headers.get("Content-Type", "")

                if "text/html" in content_type.lower():
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipped HTML response: page={row['page']} row={row['row']} file={file_name}"
                        )
                    )
                    logger.warning(
                        "Skipped HTML response while downloading purchase quote file file=%s page=%s row=%s",
                        file_name,
                        row["page"],
                        row["row"],
                    )
                    skipped += 1
                    continue

                local_path.write_bytes(file_response.content)
                PurchaseQuoteDownload.objects.update_or_create(
                    file_name=file_name,
                    defaults={
                        "quote_date": quote_date,
                        "local_path": str(local_path),
                        "file_extension": suffix,
                        "file_size": local_path.stat().st_size,
                        "source_page": row["page"],
                        "source_row": row["row"],
                        "downloaded_at": timezone.now(),
                        "status": "downloaded",
                        "error_message": None,
                    },
                )
                logger.info(
                    "Downloaded purchase quote file file=%s local_path=%s quote_date=%s page=%s row=%s size=%s",
                    file_name,
                    local_path,
                    row["quote_date_text"],
                    row["page"],
                    row["row"],
                    local_path.stat().st_size,
                )
                downloaded += 1
                new_files.append(file_name)

                if quote_date and (
                    newest_downloaded_at is None or quote_date > newest_downloaded_at
                ):
                    newest_downloaded_at = quote_date

            except Exception as exc:
                failed += 1
                logger.exception(
                    "Failed downloading purchase quote file file=%s page=%s row=%s",
                    file_name,
                    row["page"],
                    row["row"],
                )
                self.stdout.write(
                    self.style.ERROR(
                        f"Failed: page={row['page']} row={row['row']} file={file_name} error={exc}"
                    )
                )

        if new_files:
            report_path = download_dir / "new_files_last_run.txt"
            report_path.write_text("\n".join(new_files), encoding="utf-8")

            self.stdout.write(
                self.style.SUCCESS(f"New file report: {report_path}")
            )
        else:
            self.stdout.write("No new files downloaded.")

        if newest_downloaded_at:
            self._save_last_synced_at(state_path, newest_downloaded_at)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Last synced at saved: {newest_downloaded_at.strftime(DATE_FORMAT)}"
                )
            )
        logger.info(
            "Purchase quote sync completed downloaded=%s skipped=%s failed=%s",
            downloaded,
            skipped,
            failed,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Downloaded: {downloaded}, skipped: {skipped}, failed: {failed}"
            )
        )

    def _login(self, session):
        response = session.get(LOGIN_PAGE_URL, timeout=120)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        token_input = soup.find("input", {"name": "_token"})

        if not token_input:
            raise RuntimeError("Login CSRF token not found.")

        token = token_input.get("value")

        login_response = session.post(
            LOGIN_POST_URL,
            data={
                "_token": token,
                "name": settings.PURCHASE_QUOTES_WEB_USERNAME,
                "password": settings.PURCHASE_QUOTES_WEB_PASSWORD,
            },
            headers={"Referer": LOGIN_PAGE_URL},
            timeout=120,
            allow_redirects=True,
        )

        login_response.raise_for_status()

    def _get_all_file_rows(self, session, debug_html=False, last_synced_at=None):
        all_rows = []
        seen = {}
        page = 1
        raw_count = 0
        duplicate_count = 0
        should_stop = False

        while True:
            url = PAGE_URL.format(page=page)

            self.stdout.write(f"Reading page {page}")

            response = session.get(url, timeout=120)
            response.raise_for_status()

            if "Sign in to start your session" in response.text:
                raise RuntimeError(
                    "Login failed. Check PURCHASE_QUOTES_WEB_USERNAME and PURCHASE_QUOTES_WEB_PASSWORD."
                )

            if debug_html and page == 1:
                self.stdout.write(response.text[:8000])

            page_rows = self._get_file_rows(response.text, page)

            if not page_rows:
                self.stdout.write(f"No files found on page {page}. Stopping.")
                break

            raw_count += len(page_rows)
            page_new_count = 0
            page_duplicate_count = 0

            for row_data in page_rows:
                file_name = row_data["file_name"]
                quote_date = row_data["quote_date"]

                if (
                    last_synced_at
                    and quote_date
                    and quote_date <= last_synced_at
                ):
                    self.stdout.write(
                        self.style.WARNING(
                            "Reached already synced date. "
                            f"page={row_data['page']} row={row_data['row']} "
                            f"date={row_data['quote_date_text']} file={file_name}"
                        )
                    )
                    should_stop = True
                    continue

                if file_name in seen:
                    duplicate_count += 1
                    page_duplicate_count += 1
                    first_seen = seen[file_name]

                    self.stdout.write(
                        self.style.WARNING(
                            "DUPLICATE: "
                            f"current page={row_data['page']}, row={row_data['row']}, file={file_name} | "
                            f"first page={first_seen['page']}, row={first_seen['row']}"
                        )
                    )
                    continue

                seen[file_name] = {
                    "page": row_data["page"],
                    "row": row_data["row"],
                }

                all_rows.append(row_data)
                page_new_count += 1

            self.stdout.write(
                f"Page {page}: raw={len(page_rows)}, "
                f"new={page_new_count}, duplicate={page_duplicate_count}"
            )

            if should_stop:
                break

            page += 1

        self.stdout.write(
            self.style.WARNING(
                f"TOTAL: raw={raw_count}, duplicates={duplicate_count}, unique={len(all_rows)}"
            )
        )

        return all_rows

    def _get_file_rows(self, html, page):
        soup = BeautifulSoup(html, "html.parser")
        rows = []

        table_rows = soup.find_all("tr")

        for row_number, tr in enumerate(table_rows, start=1):
            cells = tr.find_all("td")

            if not cells:
                continue

            quote_date_text = self._find_date_text(cells)
            quote_date = self._parse_date(quote_date_text)

            file_name = self._find_file_name(tr)

            if not file_name:
                continue

            rows.append(
                {
                    "page": page,
                    "row": row_number,
                    "file_name": file_name,
                    "quote_date_text": quote_date_text,
                    "quote_date": quote_date,
                }
            )

        return rows

    def _find_file_name(self, tr):
        for link in tr.find_all("a", href=True):
            href = link["href"]
            parsed = urlparse(urljoin(LIST_URL, href))
            query = parse_qs(parsed.query)

            file_value = query.get("file")

            if file_value:
                return Path(file_value[0]).name.strip()

            text = link.get_text(strip=True)
            suffix = Path(text).suffix.lower()

            if suffix:
                return Path(text).name.strip()

        return ""

    def _find_date_text(self, cells):
        for cell in cells:
            text = cell.get_text(" ", strip=True)

            if self._parse_date(text):
                return text

        return ""

    def _parse_date(self, value):
        text = str(value or "").strip()

        if not text:
            return None

        try:
            return datetime.strptime(text, DATE_FORMAT)
        except ValueError:
            return None

    def _load_last_synced_at(self, state_path):
        if not state_path.exists():
            return None

        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            value = data.get("last_synced_at")

            if not value:
                return None

            return datetime.strptime(value, DATE_FORMAT)

        except Exception:
            return None

    def _save_last_synced_at(self, state_path, value):
        state_path.write_text(
            json.dumps(
                {
                    "last_synced_at": value.strftime(DATE_FORMAT),
                },
                indent=2,
            ),
            encoding="utf-8",
        )