import os
import sys
import threading
from datetime import datetime, timedelta

from django.apps import AppConfig
from django.conf import settings
from django.utils import timezone


class ApiConfig(AppConfig):
    name = 'api'

    def ready(self):
        if not self._should_start_market_scheduler():
            return
        timer = threading.Timer(5, self._market_scheduler_loop)
        timer.daemon = True
        timer.start()

    def _should_start_market_scheduler(self):
        if os.environ.get("RUN_MAIN") == "false":
            return False
        if any(command in sys.argv for command in ["test", "migrate", "makemigrations", "check", "fetch_market_prices"]):
            return False
        return bool(getattr(settings, "MARKET_PRICE_FETCH_ENABLED", True) and getattr(settings, "METALS_DEV_API_KEY", ""))

    def _market_scheduler_loop(self):
        from api.models import CommodityFetchLog
        from api.services.market_price_service import fetch_current_market_prices

        stale_hours = getattr(settings, "MARKET_PRICE_STALE_HOURS", 48)
        fetch_time = getattr(settings, "MARKET_PRICE_FETCH_TIME", "09:00")
        while True:
            now = timezone.now()
            latest_success = CommodityFetchLog.objects.filter(status="success").order_by("-created_at").first()
            due_by_staleness = not latest_success or latest_success.created_at <= now - timedelta(hours=stale_hours)
            due_by_time = now.strftime("%H:%M") >= fetch_time
            if due_by_staleness and due_by_time:
                fetch_current_market_prices()
            next_run = self._next_run_at(now, fetch_time)
            sleep_seconds = max(300, min(86400, (next_run - now).total_seconds()))
            threading.Event().wait(sleep_seconds)

    def _next_run_at(self, now, fetch_time):
        try:
            hour, minute = [int(part) for part in fetch_time.split(":", 1)]
        except ValueError:
            hour, minute = 9, 0
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
