import io
import logging
import mimetypes
import os
import posixpath
from pathlib import PurePosixPath

import zipfile

logger = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".msg", ".eml", ".csv", ".txt", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
MAX_FILES = 200
MAX_TOTAL_SIZE = 500 * 1024 * 1024
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_NESTING = 3


def _safe_name(name):
    value = str(name or "").replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return "/".join(part for part in path.parts if part not in {"", "."}) or None


def _archive_kind(name, data):
    lower = str(name or "").lower()
    if data.startswith(b"PK\x03\x04") or lower.endswith(".zip"):
        return "zip"
    if data.startswith(b"Rar!\x1a\x07") or lower.endswith(".rar"):
        return "rar"
    return None


def extract_archive(data, name, depth=0):
    if depth >= MAX_NESTING:
        return [], [{"file_name": name, "reason": "Maximum archive nesting exceeded"}]
    kind = _archive_kind(name, data)
    if not kind:
        return [], [{"file_name": name, "reason": "Unsupported archive format"}]
    if kind == "rar":
        try:
            import rarfile
            # Windows deployments use 7-Zip as the RAR extraction backend.
            # Keep the path configurable through RAR_TOOL when the server
            # installs 7-Zip in a different location.
            rarfile.SEVENZIP_TOOL = os.getenv(
                "RAR_TOOL",
                r"C:\Program Files\7-Zip\7z.exe",
            )
            logger.info(
                "RAR extraction starting file=%s tool=%s exists=%s size=%s",
                name,
                rarfile.SEVENZIP_TOOL,
                os.path.exists(rarfile.SEVENZIP_TOOL),
                len(data),
            )
        except ImportError as exc:
            logger.exception("RAR extraction dependency missing file=%s", name)
            return [], [{"file_name": name, "reason": "RAR support requires the rarfile package and an installed unrar/bsdtar backend"}]
        try:
            # Select the 7-Zip command adapter; UNRAR_TOOL uses incompatible
            # switches and causes 7z help text to be read as member bytes.
            rarfile.tool_setup(unrar=False, unar=False, bsdtar=False,
                               sevenzip=True, sevenzip2=False, force=True)
            archive = rarfile.RarFile(io.BytesIO(data))
            infos = archive.infolist()
            logger.info("RAR archive opened file=%s members=%s", name, len(infos))
        except Exception as exc:
            logger.exception(
                "RAR archive open/list failed file=%s tool=%s",
                name,
                getattr(rarfile, "SEVENZIP_TOOL", None),
            )
            return [], [{"file_name": name, "reason": f"Invalid or password-protected RAR: {exc}"}]
        reader = lambda info: archive.read(info)
    else:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            infos = archive.infolist()
        except (zipfile.BadZipFile, OSError) as exc:
            return [], [{"file_name": name, "reason": f"Invalid or password-protected ZIP: {exc}"}]
        reader = lambda info: archive.read(info)
    if len(infos) > MAX_FILES:
        return [], [{"file_name": name, "reason": "Archive contains too many files"}]
    total = sum(int(getattr(info, "file_size", 0) or 0) for info in infos)
    if total > MAX_TOTAL_SIZE:
        return [], [{"file_name": name, "reason": "Archive extracted size exceeds limit"}]
    extracted, skipped = [], []
    for info in infos:
        if getattr(info, "is_dir", lambda: False)():
            continue
        safe = _safe_name(getattr(info, "filename", ""))
        size = int(getattr(info, "file_size", 0) or 0)
        suffix = (posixpath.splitext(safe or "")[1]).lower()
        if not safe:
            skipped.append({"file_name": getattr(info, "filename", ""), "reason": "Unsafe archive path"})
            continue
        if size <= 0 or size > MAX_FILE_SIZE:
            skipped.append({"file_name": safe, "reason": "Empty or oversized archive member"})
            continue
        if suffix in {".zip", ".rar"}:
            nested, nested_skipped = extract_archive(reader(info), safe, depth + 1)
            extracted.extend(nested)
            skipped.extend(nested_skipped)
            continue
        if suffix not in SUPPORTED:
            skipped.append({"file_name": safe, "reason": "Unsupported inner file type"})
            continue
        try:
            extracted.append((safe, reader(info)))
        except Exception as exc:
            logger.exception(
                "RAR member extraction failed archive=%s member=%s",
                name,
                safe,
            )
            skipped.append({"file_name": safe, "reason": f"Could not extract member: {exc}"})
    try:
        archive.close()
    except Exception:
        pass
    return extracted, skipped
