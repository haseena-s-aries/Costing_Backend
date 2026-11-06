from ftplib import FTP, FTP_TLS, error_perm
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from api.constants import SUPPORTED_EXTENSIONS


class Command(BaseCommand):
    help = "Download quotation files from Purchase SW FTP."

    def add_arguments(self, parser):
        parser.add_argument(
            "--secure",
            action="store_true",
            help="Use FTPS instead of normal FTP.",
        )
        parser.add_argument(
            "--list-only",
            action="store_true",
            help="Only list remote files, do not download.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Maximum number of new files to download. 0 means no limit.",
        )

    def handle(self, *args, **options):
        ftp = self._connect(use_secure=options["secure"])

        remote_dir = settings.PURCHASE_QUOTES_FTP_DIR
        if remote_dir:
            ftp.cwd(remote_dir)

        self.stdout.write(f"Connected FTP folder: {ftp.pwd()}")

        remote_files = self._list_files_recursive(ftp)

        if not remote_files:
            self.stdout.write(self.style.WARNING("No files found."))
            ftp.quit()
            return

        self.stdout.write(f"Found {len(remote_files)} file(s).")

        if options["list_only"]:
            for remote_path in remote_files:
                self.stdout.write(remote_path)
            ftp.quit()
            return

        download_dir = Path(settings.PURCHASE_QUOTES_DOWNLOAD_DIR)
        download_dir.mkdir(parents=True, exist_ok=True)

        downloaded = 0
        skipped = 0
        limit = options["limit"]

        for remote_path in remote_files:
            file_name = Path(remote_path).name
            suffix = Path(file_name).suffix.lower()

            if suffix not in SUPPORTED_EXTENSIONS:
                self.stdout.write(f"Skipping unsupported file: {remote_path}")
                skipped += 1
                continue

            local_path = download_dir / file_name

            if local_path.exists() and local_path.stat().st_size > 0:
                self.stdout.write(f"Already downloaded: {file_name}")
                skipped += 1
                continue

            if limit and downloaded >= limit:
                self.stdout.write(
                    self.style.WARNING(
                        f"Limit reached. Downloaded {downloaded} new file(s)."
                    )
                )
                break

            self.stdout.write(f"Downloading: {remote_path}")

            with local_path.open("wb") as local_file:
                ftp.retrbinary(f"RETR {remote_path}", local_file.write)

            downloaded += 1

        ftp.quit()

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Downloaded: {downloaded}, skipped: {skipped}"
            )
        )

    def _connect(self, use_secure=False):
        ftp_class = FTP_TLS if use_secure else FTP

        ftp = ftp_class(
            settings.PURCHASE_QUOTES_FTP_HOST,
            timeout=60,
        )

        ftp.login(
            settings.PURCHASE_QUOTES_FTP_USER,
            settings.PURCHASE_QUOTES_FTP_PASSWORD,
        )

        if use_secure:
            ftp.prot_p()

        return ftp

    def _list_files_recursive(self, ftp, folder=""):
        files = []
        current_folder = ftp.pwd()

        if folder:
            ftp.cwd(folder)

        try:
            names = ftp.nlst()
        except error_perm:
            ftp.cwd(current_folder)
            return files

        for name in names:
            clean_name = name.rstrip("/")
            base_name = Path(clean_name).name

            if base_name in {"", ".", ".."}:
                continue

            try:
                ftp.cwd(clean_name)
                ftp.cwd("..")

                child_files = self._list_files_recursive(ftp, clean_name)
                files.extend(child_files)

            except error_perm:
                files.append(clean_name)

        ftp.cwd(current_folder)
        return files