import logging
import time

from django.conf import settings


request_logger = logging.getLogger("api.request")


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.slow_ms = int(getattr(settings, "SLOW_REQUEST_LOG_MS", 5000))

    def __call__(self, request):
        started_at = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            request_logger.exception(
                "EXCEPTION method=%s path=%s duration_ms=%s user=%s remote_addr=%s query=%s",
                request.method,
                request.get_full_path(),
                duration_ms,
                getattr(request, "user", None),
                self._remote_addr(request),
                request.META.get("QUERY_STRING", ""),
            )
            raise

        duration_ms = int((time.monotonic() - started_at) * 1000)
        status_code = getattr(response, "status_code", None)

        if status_code and status_code >= 500:
            request_logger.error(
                "SERVER_ERROR method=%s path=%s status=%s duration_ms=%s user=%s remote_addr=%s query=%s",
                request.method,
                request.get_full_path(),
                status_code,
                duration_ms,
                getattr(request, "user", None),
                self._remote_addr(request),
                request.META.get("QUERY_STRING", ""),
            )
        elif duration_ms >= self.slow_ms:
            request_logger.warning(
                "SLOW_REQUEST method=%s path=%s status=%s duration_ms=%s user=%s remote_addr=%s query=%s",
                request.method,
                request.get_full_path(),
                status_code,
                duration_ms,
                getattr(request, "user", None),
                self._remote_addr(request),
                request.META.get("QUERY_STRING", ""),
            )
        else:
            request_logger.info(
                "REQUEST method=%s path=%s status=%s duration_ms=%s",
                request.method,
                request.path,
                status_code,
                duration_ms,
            )

        return response

    def _remote_addr(self, request):
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
        return request.META.get("REMOTE_ADDR", "")
