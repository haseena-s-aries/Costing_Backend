
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from api.models import CommodityBenchmark, CommodityFetchLog, CommodityPriceHistory, Product

logger = logging.getLogger(__name__)

PROVIDER = "metals_dev"
YAHOO_PROVIDER = "yahoo_finance"
HRC_SYMBOL = "HRC=F"
HRC_CHART_ENDPOINT = "/v8/finance/chart/HRC%3DF"
SHORT_TON_TO_METRIC_TONNE = Decimal("0.90718474")
INDUSTRIAL_CANDIDATES = {
    "copper": "Copper",
    "aluminum": "Aluminium",
}
PRODUCT_ALIASES = {
    "copper": ["copper", "cu"],
    "aluminum": ["aluminium", "aluminum", "al "],
}


class MarketPriceError(Exception):
    pass


def fetch_current_market_prices(force=False):
    if not getattr(settings, "MARKET_PRICE_FETCH_ENABLED", True) and not force:
        return {"status": "disabled", "created": 0, "request_count": 0}
    metals_result = MetalsDevMarketPriceService().fetch_current_market_prices()
    hrc_result = YahooHRCMarketPriceService().fetch_latest(force=force)
    return {**metals_result, "hrc": hrc_result}


class MetalsDevMarketPriceService:
    def __init__(self):
        self.api_key = getattr(settings, "METALS_DEV_API_KEY", "")
        self.base_url = getattr(settings, "METALS_DEV_BASE_URL", "https://api.metals.dev").rstrip("/")
        self.request_count = 0

    def fetch_current_market_prices(self):
        if not self.api_key:
            return self._log("skipped", [], {"reason": "METALS_DEV_API_KEY is empty"}, "API key not configured")
        requested = []
        try:
            latest = self._get_json("/v1/latest", {"currency": "USD", "unit": "mt"})
            currencies = self._get_json("/v1/currencies", {"base": "USD"})
            rates = latest.get("metals") or latest.get("rates") or {}
            supported = self._sync_supported_benchmarks(rates)
            requested = [item.provider_symbol for item in supported]
            usd_aed = self._usd_to_aed(currencies)
            created = self._save_prices(supported, rates, latest, usd_aed)
            self._auto_map_products()
            return self._log("success", requested, {"created": created, "supported": requested}, None)
        except MarketPriceError as exc:
            return self._log("failed", requested, {"preserved_last_valid": True}, str(exc))

    def _get_json(self, endpoint, params):
        query = urlencode({**params, "api_key": self.api_key})
        url = f"{self.base_url}{endpoint}?{query}"
        request = Request(url, headers={"Accept": "application/json"})
        self.request_count += 1
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="ignore")[:500]
            raise MarketPriceError(f"Metals.Dev HTTP {exc.code}: {message}") from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise MarketPriceError(f"Metals.Dev request failed: {exc}") from exc
        if payload.get("status") == "failure":
            raise MarketPriceError(str(payload.get("error") or payload))
        return payload

    def _sync_supported_benchmarks(self, rates):
        supported = []
        seen_symbols = set()
        for symbol, display in INDUSTRIAL_CANDIDATES.items():
            if symbol not in rates:
                continue
            benchmark, _ = CommodityBenchmark.objects.update_or_create(
                provider=PROVIDER,
                provider_symbol=symbol,
                defaults={
                    "display_name": display,
                    "benchmark_name": f"Metals.Dev {display}",
                    "metal_type": display,
                    "region": "Global",
                    "default_unit": "mt",
                    "is_supported": True,
                    "is_active": True,
                    "metadata": {"source": "latest", "provider_symbol": symbol},
                },
            )
            supported.append(benchmark)
            seen_symbols.add(symbol)
        CommodityBenchmark.objects.filter(provider=PROVIDER).exclude(provider_symbol__in=seen_symbols).update(is_supported=False, is_active=False)
        return supported

    def _save_prices(self, benchmarks, rates, latest, usd_aed):
        timestamp = self._parse_timestamp(latest.get("timestamp"))
        market_date = (timestamp or timezone.now()).date()
        created = 0
        for benchmark in benchmarks:
            raw = self._decimal(rates.get(benchmark.provider_symbol))
            if raw is None:
                continue
            usd_mt = raw
            aed_mt = (usd_mt * usd_aed).quantize(Decimal("0.00000001"))
            aed_kg = (aed_mt / Decimal("1000")).quantize(Decimal("0.00000001"))
            try:
                with transaction.atomic():
                    _, was_created = CommodityPriceHistory.objects.get_or_create(
                        benchmark=benchmark,
                        market_date=market_date,
                        defaults={
                            "source_timestamp": timestamp,
                            "provider": PROVIDER,
                            "provider_symbol": benchmark.provider_symbol,
                            "raw_price": raw,
                            "raw_currency": latest.get("currency") or "USD",
                            "raw_unit": latest.get("unit") or "mt",
                            "usd_per_metric_tonne": usd_mt,
                            "usd_aed_exchange_rate": usd_aed,
                            "aed_per_metric_tonne": aed_mt,
                            "aed_per_kg": aed_kg,
                            "response_metadata": {"status": latest.get("status"), "unit": latest.get("unit"), "currency": latest.get("currency")},
                        },
                    )
                    created += int(was_created)
            except IntegrityError:
                continue
        return created
    def _usd_to_aed(self, currencies):
        value = (currencies.get("currencies") or {}).get("AED")
        rate = self._decimal(value)
        if rate is None or rate <= 0:
            raise MarketPriceError("AED conversion rate unavailable from Metals.Dev")
        # Metals.Dev currencies endpoint with base=USD returns USD value per currency in docs; invert if needed.
        return (Decimal("1") / rate).quantize(Decimal("0.00000001")) if rate < 1 else rate

    def _auto_map_products(self):
        benchmarks = {b.provider_symbol: b for b in CommodityBenchmark.objects.filter(provider=PROVIDER, is_active=True, is_supported=True)}
        for product in Product.objects.filter(is_active=True, commodity_benchmark__isnull=True)[:5000]:
            text = f" {product.product_name or ''} {product.item_code or ''} {product.part_number or ''} {product.brand or ''} ".lower()
            for symbol, aliases in PRODUCT_ALIASES.items():
                if symbol in benchmarks and any(alias in text for alias in aliases):
                    product.commodity_benchmark = benchmarks[symbol]
                    product.save(update_fields=["commodity_benchmark", "updated_at"])
                    break

    def _parse_timestamp(self, value):
        if not value:
            return timezone.now()
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return timezone.now()

    def _decimal(self, value):
        try:
            return Decimal(str(value).replace(",", "").strip())
        except (InvalidOperation, AttributeError):
            return None

    def _log(self, status, requested, summary, error):
        CommodityFetchLog.objects.create(
            provider=PROVIDER,
            status=status,
            requested_symbols=requested,
            response_summary=summary,
            error_message=error,
            request_count=self.request_count,
        )
        if error:
            logger.warning("Commodity market fetch failed: %s", error)
        return {"status": status, **summary, "request_count": self.request_count}


class YahooHRCMarketPriceService:
    source = YAHOO_PROVIDER
    symbol = HRC_SYMBOL
    commodity_name = "Hot Rolled Coil Steel"
    benchmark_name = "US Midwest HRC Futures"
    source_market = "CME/COMEX"

    def __init__(self):
        self.base_url = getattr(settings, "YAHOO_FINANCE_BASE_URL", "https://query1.finance.yahoo.com").rstrip("/")
        self.timeout = int(getattr(settings, "YAHOO_FINANCE_TIMEOUT", 20))
        self.request_count = 0

    def fetch_latest(self, force=False):
        latest_saved = self.latest_saved()
        try:
            payload = self._get_chart_json()
            point = self._latest_valid_close(payload)
            if not point:
                return self._stale_result("No valid HRC close in Yahoo Finance response")

            market_date, usd_per_short_ton, source_timestamp = point
            if latest_saved and latest_saved.market_date == market_date and not force:
                return {
                    "status": "success",
                    "created": 0,
                    "updated": 0,
                    "market_date": market_date.isoformat(),
                    "is_stale": False,
                    "request_count": self.request_count,
                }

            usd_to_aed = self._usd_to_aed_rate()
            price, was_created = self._save_price(
                market_date=market_date,
                source_timestamp=source_timestamp,
                usd_per_short_ton=usd_per_short_ton,
                usd_to_aed=usd_to_aed,
                raw_response=self._raw_metadata(payload),
            )
            return {
                "status": "success",
                "created": int(was_created),
                "updated": int(not was_created),
                "market_date": price.market_date.isoformat(),
                "is_stale": False,
                "request_count": self.request_count,
            }
        except MarketPriceError as exc:
            return self._stale_result(str(exc))

    def latest_response(self, force=False):
        latest = self.latest_saved()
        if latest and not force and latest.updated_at.date() == timezone.now().date():
            return self.serialize(latest, is_stale=False)
        result = self.fetch_latest(force=force)
        latest = self.latest_saved()
        return self.serialize(latest, is_stale=bool(result.get("is_stale")))

    def history_response(self, days=30):
        benchmark = self.get_benchmark()
        limit = max(1, min(int(days or 30), 365))
        prices = CommodityPriceHistory.objects.filter(
            benchmark=benchmark,
        ).order_by("-market_date", "-created_at")[:limit]
        return [self.serialize(price, is_stale=False) for price in prices]

    def get_benchmark(self):
        benchmark, _ = CommodityBenchmark.objects.update_or_create(
            provider=self.source,
            provider_symbol=self.symbol,
            defaults={
                "display_name": self.commodity_name,
                "benchmark_name": self.benchmark_name,
                "metal_type": "Steel",
                "region": "US Midwest",
                "default_unit": "mt",
                "is_supported": True,
                "is_active": True,
                "metadata": {
                    "source": self.source,
                    "source_symbol": self.symbol,
                    "source_market": self.source_market,
                    "is_delayed": True,
                    "category": "MARKET_PRICE",
                    "original_unit": "SHORT_TON",
                    "normalized_unit": "METRIC_TONNE",
                },
            },
        )
        return benchmark

    def latest_saved(self):
        return CommodityPriceHistory.objects.filter(
            benchmark=self.get_benchmark(),
        ).order_by("-market_date", "-created_at").first()

    def _get_chart_json(self):
        url = f"{self.base_url}{HRC_CHART_ENDPOINT}?range=1mo&interval=1d"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "PriceIntelligence/1.0",
            },
        )
        self.request_count += 1
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if getattr(response, "status", 200) >= 400:
                    raise MarketPriceError(f"Yahoo Finance HTTP {response.status}")
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise MarketPriceError(f"Yahoo Finance HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise MarketPriceError("Yahoo Finance request timed out") from exc
        except (URLError, ValueError, json.JSONDecodeError) as exc:
            raise MarketPriceError(f"Yahoo Finance request failed: {exc}") from exc

    def _latest_valid_close(self, payload):
        try:
            result = (payload.get("chart") or {}).get("result") or []
            first = result[0]
            timestamps = first.get("timestamp") or []
            quote = ((first.get("indicators") or {}).get("quote") or [])[0]
            closes = quote.get("close") or []
        except (AttributeError, IndexError, TypeError) as exc:
            raise MarketPriceError("Malformed Yahoo Finance chart response") from exc

        if not timestamps or not closes:
            raise MarketPriceError("Yahoo Finance chart response has no HRC series")

        for timestamp, close in reversed(list(zip(timestamps, closes))):
            price = self._decimal(close)
            if price is None or price <= 0:
                continue

            source_timestamp = datetime.fromtimestamp(int(timestamp), tz=datetime_timezone.utc)
            return source_timestamp.date(), price, source_timestamp

        return None

    def _usd_to_aed_rate(self):
        api_key = getattr(settings, "METALS_DEV_API_KEY", "")
        if api_key:
            try:
                metals = MetalsDevMarketPriceService()
                currencies = metals._get_json("/v1/currencies", {"base": "USD"})
                return metals._usd_to_aed(currencies)
            except MarketPriceError:
                logger.warning("Could not fetch USD/AED for HRC; trying last stored exchange rate")

        latest_exchange = CommodityPriceHistory.objects.exclude(
            usd_aed_exchange_rate__isnull=True,
        ).order_by("-market_date", "-created_at").first()
        if latest_exchange and latest_exchange.usd_aed_exchange_rate > 0:
            return latest_exchange.usd_aed_exchange_rate

        raise MarketPriceError("USD/AED conversion rate unavailable")

    def _save_price(self, *, market_date, source_timestamp, usd_per_short_ton, usd_to_aed, raw_response):
        benchmark = self.get_benchmark()
        usd_per_metric_tonne = (usd_per_short_ton / SHORT_TON_TO_METRIC_TONNE).quantize(Decimal("0.00000001"))
        aed_per_metric_tonne = (usd_per_metric_tonne * usd_to_aed).quantize(Decimal("0.00000001"))
        aed_per_kg = (aed_per_metric_tonne / Decimal("1000")).quantize(Decimal("0.00000001"))

        with transaction.atomic():
            price, was_created = CommodityPriceHistory.objects.update_or_create(
                benchmark=benchmark,
                market_date=market_date,
                defaults={
                    "source_timestamp": source_timestamp,
                    "provider": self.source,
                    "provider_symbol": self.symbol,
                    "raw_price": usd_per_short_ton,
                    "raw_currency": "USD",
                    "raw_unit": "SHORT_TON",
                    "usd_per_metric_tonne": usd_per_metric_tonne,
                    "usd_aed_exchange_rate": usd_to_aed,
                    "aed_per_metric_tonne": aed_per_metric_tonne,
                    "aed_per_kg": aed_per_kg,
                    "response_metadata": {
                        **raw_response,
                        "symbol": self.symbol,
                        "commodity_name": self.commodity_name,
                        "benchmark_name": self.benchmark_name,
                        "category": "MARKET_PRICE",
                        "source": self.source,
                        "source_market": self.source_market,
                        "original_currency": "USD",
                        "original_unit": "SHORT_TON",
                        "normalized_currency": "AED",
                        "normalized_unit": "METRIC_TONNE",
                        "is_delayed": True,
                    },
                },
            )
        return price, was_created

    def _stale_result(self, error):
        latest = self.latest_saved()
        CommodityFetchLog.objects.create(
            provider=self.source,
            status="stale" if latest else "failed",
            requested_symbols=[self.symbol],
            response_summary={
                "preserved_last_valid": bool(latest),
                "is_stale": bool(latest),
            },
            error_message=error,
            request_count=self.request_count,
        )
        logger.warning("HRC market fetch failed; using stale=%s: %s", bool(latest), error)
        return {
            "status": "stale" if latest else "failed",
            "created": 0,
            "updated": 0,
            "is_stale": bool(latest),
            "error": error,
            "request_count": self.request_count,
        }

    def _raw_metadata(self, payload):
        result = ((payload.get("chart") or {}).get("result") or [{}])[0]
        meta = result.get("meta") or {}
        return {
            "chart_meta": {
                "currency": meta.get("currency"),
                "symbol": meta.get("symbol"),
                "exchange_name": meta.get("exchangeName"),
                "instrument_type": meta.get("instrumentType"),
                "regular_market_time": meta.get("regularMarketTime"),
            }
        }

    def serialize(self, price, is_stale=False):
        if not price:
            return None
        metadata = price.response_metadata or {}
        return {
            "symbol": self.symbol,
            "commodity_name": self.commodity_name,
            "benchmark_name": self.benchmark_name,
            "category": "MARKET_PRICE",
            "price": str(price.aed_per_kg.quantize(Decimal("0.01"))),
            "currency": "AED",
            "unit": "KG",
            "original_price": str(price.raw_price.quantize(Decimal("0.01"))),
            "original_currency": "USD",
            "original_unit": "SHORT_TON",
            "usd_to_aed_rate": str(price.usd_aed_exchange_rate),
            "market_date": price.market_date.isoformat(),
            "source": "YAHOO_FINANCE",
            "source_symbol": self.symbol,
            "source_market": self.source_market,
            "is_delayed": bool(metadata.get("is_delayed", True)),
            "is_stale": bool(is_stale),
            "fetched_at": price.updated_at.isoformat() if price.updated_at else None,
            "commodity_id": price.benchmark_id,
            "commodity_price_history_id": price.id,
            "aed_per_kg": str(price.aed_per_kg),
        }

    def _decimal(self, value):
        try:
            return Decimal(str(value).replace(",", "").strip())
        except (InvalidOperation, AttributeError):
            return None
