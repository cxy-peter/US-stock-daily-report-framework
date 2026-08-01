"""Market-data providers for the daily research pipeline."""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests


@dataclass
class Quote:
    ticker: str
    price: float
    market_cap: float | None
    closes: pd.Series
    volumes: pd.Series
    currency: str = "USD"
    asset_type: str = "unknown"
    as_of: str = ""
    source: str = ""


class Provider:
    def get(
        self,
        ticker: str,
        period: str = "1y",
        asset_type_hint: str | None = None,
    ) -> Quote:  # pragma: no cover - interface
        raise NotImplementedError


def _period_start(period: str) -> dt.date:
    today = dt.date.today()
    text = (period or "1y").strip().lower()
    try:
        if text.endswith("mo"):
            return today - dt.timedelta(days=31 * int(text[:-2] or "1"))
        if text.endswith("y"):
            return today - dt.timedelta(days=365 * int(text[:-1] or "1"))
        if text.endswith("d"):
            return today - dt.timedelta(days=int(text[:-1] or "365"))
    except ValueError:
        pass
    return today - dt.timedelta(days=365)


def _to_float(value: Any) -> float:
    return float(str(value).replace("$", "").replace(",", "").strip())


def _compact_number(value: Any) -> float | None:
    if value in (None, "", "N/A", "--"):
        return None
    text = str(value).strip().upper().replace("$", "").replace(",", "")
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*([KMBT]?)", text)
    if not match:
        try:
            parsed = float(text)
            return parsed if math.isfinite(parsed) else None
        except ValueError:
            return None
    number = float(match.group(1))
    return number * {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[match.group(2)]


class NasdaqProvider(Provider):
    HISTORY_API = "https://api.nasdaq.com/api/quote/{ticker}/historical"
    SUMMARY_API = "https://api.nasdaq.com/api/quote/{ticker}/summary"
    ASSET_CLASSES = ("stocks", "etf")
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; daily-research-agent/2.0)",
        "Accept": "application/json",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def _market_cap(self, ticker: str, asset_class: str) -> float | None:
        if asset_class != "stocks":
            return None
        response = self.session.get(
            self.SUMMARY_API.format(ticker=ticker.upper()),
            params={"assetclass": asset_class},
            headers=self.HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        summary = ((response.json().get("data") or {}).get("summaryData") or {})
        for key in ("MarketCap", "Market Capitalization"):
            parsed = _compact_number((summary.get(key) or {}).get("value"))
            if parsed is not None:
                return parsed
        return None

    def get(
        self,
        ticker: str,
        period: str = "1y",
        asset_type_hint: str | None = None,
    ) -> Quote:
        start, end = _period_start(period), dt.date.today()
        errors: list[str] = []
        asset_classes = {
            "stock": ("stocks",),
            "etf": ("etf",),
        }.get(str(asset_type_hint or "").lower(), self.ASSET_CLASSES)
        for asset_class in asset_classes:
            try:
                response = self.session.get(
                    self.HISTORY_API.format(ticker=ticker.upper()),
                    params={
                        "assetclass": asset_class,
                        "fromdate": start.isoformat(),
                        "todate": end.isoformat(),
                        "limit": "9999",
                    },
                    headers=self.HEADERS,
                    timeout=20,
                )
                response.raise_for_status()
                rows = ((((response.json().get("data") or {}).get("tradesTable") or {}).get("rows")) or [])
                records: list[dict[str, Any]] = []
                for row in rows:
                    try:
                        records.append(
                            {
                                "Date": pd.to_datetime(row["date"], format="%m/%d/%Y"),
                                "Close": _to_float(row["close"]),
                                "Volume": _to_float(row["volume"]),
                            }
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                if not records:
                    errors.append(f"{asset_class}: no rows")
                    continue
                frame = pd.DataFrame(records).sort_values("Date").set_index("Date")
                closes = frame["Close"].dropna().astype(float)
                volumes = frame["Volume"].reindex(closes.index).fillna(0).astype(float)
                try:
                    market_cap = self._market_cap(ticker, asset_class)
                except Exception:
                    market_cap = None
                return Quote(
                    ticker=ticker.upper(),
                    price=float(closes.iloc[-1]),
                    market_cap=market_cap,
                    closes=closes,
                    volumes=volumes,
                    currency="USD",
                    asset_type="stock" if asset_class == "stocks" else "etf",
                    as_of=str(closes.index[-1].date()),
                    source="nasdaq",
                )
            except Exception as exc:
                errors.append(f"{asset_class}: {type(exc).__name__}")
        raise RuntimeError(f"{ticker}: Nasdaq unavailable ({'; '.join(errors)})")


class CboeIndexProvider(Provider):
    """Official daily history for the Cboe volatility indices used here."""

    HISTORY_URLS = {
        "VIX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
        "VIX3M": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv",
    }

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def get(
        self,
        ticker: str,
        period: str = "1y",
        asset_type_hint: str | None = None,
    ) -> Quote:
        symbol = ticker.upper().lstrip("^")
        url = self.HISTORY_URLS.get(symbol)
        if url is None:
            raise ValueError(f"unsupported Cboe index: {ticker}")
        response = self.session.get(
            url,
            headers={"User-Agent": "daily-research-agent/2.3"},
            timeout=20,
        )
        response.raise_for_status()
        frame = pd.read_csv(io.StringIO(response.text))
        required = {"DATE", "CLOSE"}
        if not required.issubset(frame.columns):
            raise RuntimeError(f"{symbol}: Cboe history is missing {required - set(frame.columns)}")
        frame["DATE"] = pd.to_datetime(frame["DATE"], errors="coerce")
        frame["CLOSE"] = pd.to_numeric(frame["CLOSE"], errors="coerce")
        frame = frame.dropna(subset=["DATE", "CLOSE"]).sort_values("DATE")
        frame = frame[frame["DATE"].dt.date >= _period_start(period)]
        if frame.empty:
            raise RuntimeError(f"{symbol}: Cboe returned no usable history")
        closes = frame.set_index("DATE")["CLOSE"].astype(float)
        volumes = pd.Series(0.0, index=closes.index, dtype=float)
        return Quote(
            ticker=f"^{symbol}",
            price=float(closes.iloc[-1]),
            market_cap=None,
            closes=closes,
            volumes=volumes,
            currency="USD",
            asset_type="index",
            as_of=str(pd.Timestamp(closes.index[-1]).date()),
            source="cboe",
        )


class YFinanceProvider(Provider):
    def __init__(self) -> None:
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("yfinance is not installed") from exc
        self.yf = yf

    @staticmethod
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        try:
            value = obj.fast_info.get(key, default)
            return default if value is None else value
        except Exception:
            try:
                return obj.info.get(key, default)
            except Exception:
                return default

    def get(
        self,
        ticker: str,
        period: str = "1y",
        asset_type_hint: str | None = None,
    ) -> Quote:
        obj = self.yf.Ticker(ticker)
        hist = obj.history(period=period, auto_adjust=True)
        if hist.empty:
            raise RuntimeError(f"{ticker}: yfinance returned no history")
        closes = hist["Close"].dropna().astype(float)
        volumes = hist["Volume"].reindex(closes.index).fillna(0).astype(float)
        quote_type = str(self._get(obj, "quote_type", self._get(obj, "quoteType", "unknown"))).lower()
        currency = str(self._get(obj, "currency", "USD") or "USD").upper()
        market_cap = self._get(obj, "market_cap", self._get(obj, "marketCap"))
        try:
            market_cap = float(market_cap) if market_cap else None
        except (TypeError, ValueError):
            market_cap = None
        asset_type = "etf" if "etf" in quote_type else "stock"
        if asset_type_hint in {"stock", "etf"} and asset_type != asset_type_hint:
            raise RuntimeError(
                f"{ticker}: provider asset type {asset_type} conflicts with "
                f"configured identity {asset_type_hint}"
            )
        return Quote(
            ticker=ticker.upper(),
            price=float(closes.iloc[-1]),
            market_cap=market_cap,
            closes=closes,
            volumes=volumes,
            currency=currency,
            asset_type=asset_type,
            as_of=str(pd.Timestamp(closes.index[-1]).date()),
            source="yfinance",
        )


class HybridProvider(Provider):
    """Use Nasdaq first and yfinance as fallback or market-cap enrichment."""

    def __init__(self) -> None:
        self.primary = NasdaqProvider()
        self._secondary: YFinanceProvider | None = None

    def secondary(self) -> YFinanceProvider:
        if self._secondary is None:
            self._secondary = YFinanceProvider()
        return self._secondary

    def get(
        self,
        ticker: str,
        period: str = "1y",
        asset_type_hint: str | None = None,
    ) -> Quote:
        try:
            quote = self.primary.get(ticker, period, asset_type_hint)
        except Exception as primary_error:
            try:
                return self.secondary().get(ticker, period, asset_type_hint)
            except Exception as secondary_error:
                raise RuntimeError(
                    f"{ticker}: primary={primary_error}; fallback={secondary_error}"
                ) from secondary_error
        if quote.asset_type == "stock" and quote.market_cap is None:
            try:
                fallback = self.secondary().get(ticker, period, asset_type_hint)
                if fallback.currency == "USD":
                    quote.market_cap = fallback.market_cap
            except Exception:
                pass
        return quote


class BaostockProvider(Provider):
    def __init__(self) -> None:
        try:
            import baostock as bs
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("baostock is not installed") from exc
        self.bs = bs

    @staticmethod
    def _normalize(ticker: str) -> str:
        text = ticker.lower()
        if text.startswith(("sh.", "sz.")):
            return text
        digits = re.sub(r"\D", "", text)
        if len(digits) != 6:
            raise ValueError(f"invalid A-share symbol: {ticker}")
        return ("sh." if digits.startswith(("5", "6", "9")) else "sz.") + digits

    def get(
        self,
        ticker: str,
        period: str = "1y",
        asset_type_hint: str | None = None,
    ) -> Quote:
        symbol = self._normalize(ticker)
        login = self.bs.login()
        if getattr(login, "error_code", "1") != "0":
            raise RuntimeError(f"baostock login failed: {login.error_msg}")
        try:
            query = self.bs.query_history_k_data_plus(
                symbol,
                "date,close,volume",
                start_date=_period_start(period).isoformat(),
                end_date=dt.date.today().isoformat(),
                frequency="d",
                adjustflag="2",
            )
            rows = []
            while query.error_code == "0" and query.next():
                rows.append(query.get_row_data())
            if not rows:
                raise RuntimeError(f"{ticker}: baostock returned no history")
            frame = pd.DataFrame(rows, columns=query.fields)
            frame["date"] = pd.to_datetime(frame["date"])
            frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
            frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0)
            frame = frame.dropna(subset=["close"]).set_index("date").sort_index()
            closes, volumes = frame["close"], frame["volume"]
            return Quote(
                ticker=ticker.upper(),
                price=float(closes.iloc[-1]),
                market_cap=None,
                closes=closes,
                volumes=volumes,
                currency="CNY",
                asset_type="stock",
                as_of=str(closes.index[-1].date()),
                source="baostock",
            )
        finally:
            self.bs.logout()


class MockProvider(Provider):
    """Deterministic offline provider used by CI and tests."""

    def get(
        self,
        ticker: str,
        period: str = "1y",
        asset_type_hint: str | None = None,
    ) -> Quote:
        digest = hashlib.sha256(ticker.upper().encode()).digest()
        seed = int.from_bytes(digest[:8], "big")
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=252)
        base = 25 + (seed % 175)
        drift = ((seed >> 8) % 15 - 5) / 10000
        shocks = rng.normal(drift, 0.015 + ((seed >> 16) % 8) / 1000, len(dates))
        prices = base * np.exp(np.cumsum(shocks))
        volumes = rng.integers(300_000, 4_000_000, len(dates)).astype(float)
        closes = pd.Series(prices, index=dates, dtype=float)
        volume_series = pd.Series(volumes, index=dates, dtype=float)
        inferred_asset_type = "etf" if ticker.upper() in {
            "SPY", "QQQ", "VTI", "BND", "SGOV"
        } else "stock"
        asset_type = (
            asset_type_hint
            if asset_type_hint in {"stock", "etf"}
            else inferred_asset_type
        )
        return Quote(
            ticker=ticker.upper(),
            price=float(closes.iloc[-1]),
            market_cap=None if asset_type == "etf" else float(0.5e9 + (seed % 20_000) * 1e6),
            closes=closes,
            volumes=volume_series,
            asset_type=asset_type,
            as_of=str(dates[-1].date()),
            source="mock",
        )


def snapshot_fallback_quote(row: dict, as_of: str) -> Quote | None:
    """Reconstruct a stale broker-snapshot price when live providers fail."""
    try:
        ticker = str(row["ticker"]).upper()
        shares = float(row.get("shares", 0) or 0)
        entry_price = float(row.get("entry_price", 0) or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if shares <= 0 or entry_price <= 0:
        return None
    price = None
    try:
        broker_pnl = float(row.get("broker_pnl_usd"))
        price = entry_price + broker_pnl / shares
    except (TypeError, ValueError):
        pass
    if price is None or price <= 0:
        try:
            price = entry_price * (1 + float(row.get("broker_pnl_pct")))
        except (TypeError, ValueError):
            price = entry_price
    if price <= 0:
        return None
    end = pd.Timestamp(as_of or dt.date.today().isoformat())
    dates = pd.bdate_range(end=end, periods=252)
    closes = pd.Series([price] * len(dates), index=dates, dtype=float)
    volumes = pd.Series([0.0] * len(dates), index=dates, dtype=float)
    explicit_type = str(row.get("asset_type", "")).lower()
    framework = str(row.get("framework", "")).lower()
    asset_type = (
        explicit_type
        if explicit_type in {"stock", "etf"}
        else ("etf" if "etf" in framework or framework == "cash_equivalent" else "stock")
    )
    return Quote(
        ticker=ticker,
        price=float(price),
        market_cap=None,
        closes=closes,
        volumes=volumes,
        asset_type=asset_type,
        as_of=str(end.date()),
        source="broker_snapshot_fallback",
    )
