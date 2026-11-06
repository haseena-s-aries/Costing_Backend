
from django.core.management.base import BaseCommand

from api.services.market_price_service import fetch_current_market_prices


class Command(BaseCommand):
    help = "Fetch current commodity market prices from the configured provider."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run even when MARKET_PRICE_FETCH_ENABLED=false.")

    def handle(self, *args, **options):
        result = fetch_current_market_prices(force=options["force"])
        self.stdout.write(self.style.SUCCESS(str(result)))
