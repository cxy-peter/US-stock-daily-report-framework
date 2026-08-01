"""Low-frequency SEC EDGAR collector with explicit health reporting."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

import requests


COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
SUPPORTED_FORMS = ("8-K", "10-Q", "10-K", "6-K", "20-F")


@dataclass(frozen=True)
class SecFiling:
    ticker: str
    cik: int
    form: str
    filing_date: str
    accession_number: str
    primary_document: str
    url: str
    title: str


@dataclass
class SecCollectionResult:
    status: str
    detail: str
    filings: list[SecFiling] = field(default_factory=list)


def _is_company_security(row: dict) -> bool:
    framework = str(row.get("framework", "")).lower()
    if "etf" in framework or framework in {"cash_equivalent", "hedge_etf"}:
        return False
    return bool(str(row.get("ticker", "")).strip())


def company_security_targets(rows: Iterable[dict]) -> list[dict]:
    """Return only targets for which company-level SEC filings are relevant."""

    return [row for row in rows if _is_company_security(row)]


def _cik_by_ticker(payload: dict) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for row in payload.values():
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker", "")).upper().strip()
        try:
            cik = int(row.get("cik_str"))
        except (TypeError, ValueError):
            continue
        if ticker:
            mapping[ticker] = cik
    return mapping


def collect_sec_filings(
    rows: Iterable[dict],
    *,
    session: requests.Session | None = None,
    user_agent: str,
    lookback_days: int = 120,
    max_filings_per_ticker: int = 5,
    request_pause_seconds: float = 0.11,
) -> SecCollectionResult:
    targets = company_security_targets(rows)
    if not targets:
        return SecCollectionResult("ok", "No company-security targets configured")
    http = session or requests.Session()
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }
    try:
        response = http.get(COMPANY_TICKERS_URL, headers=headers, timeout=20)
        response.raise_for_status()
        ticker_map = _cik_by_ticker(response.json())
    except Exception as exc:
        return SecCollectionResult(
            "error",
            f"SEC company-ticker lookup failed: {type(exc).__name__}: {exc}",
        )

    cutoff = date.today() - timedelta(days=max(1, int(lookback_days)))
    filings: list[SecFiling] = []
    failures: list[str] = []
    queried = 0
    for row in targets:
        ticker = str(row.get("ticker", "")).upper().strip()
        cik = ticker_map.get(ticker)
        if cik is None:
            failures.append(f"{ticker}: CIK not found")
            continue
        try:
            response = http.get(
                SUBMISSIONS_URL.format(cik=cik),
                headers={**headers, "Host": "data.sec.gov"},
                timeout=20,
            )
            response.raise_for_status()
            recent = ((response.json().get("filings") or {}).get("recent") or {})
            queried += 1
        except Exception as exc:
            failures.append(f"{ticker}: {type(exc).__name__}: {exc}")
            continue
        if request_pause_seconds > 0:
            time.sleep(request_pause_seconds)

        forms = list(recent.get("form") or [])
        dates = list(recent.get("filingDate") or [])
        accessions = list(recent.get("accessionNumber") or [])
        documents = list(recent.get("primaryDocument") or [])
        accepted = 0
        for form, filing_date, accession, document in zip(forms, dates, accessions, documents):
            if form not in SUPPORTED_FORMS:
                continue
            try:
                parsed_date = date.fromisoformat(str(filing_date))
            except ValueError:
                continue
            if parsed_date < cutoff:
                continue
            accession_clean = str(accession).replace("-", "")
            url = ARCHIVES_URL.format(
                cik=cik,
                accession=accession_clean,
                document=str(document),
            )
            filings.append(
                SecFiling(
                    ticker=ticker,
                    cik=cik,
                    form=str(form),
                    filing_date=parsed_date.isoformat(),
                    accession_number=str(accession),
                    primary_document=str(document),
                    url=url,
                    title=f"{ticker} {form} filed {parsed_date.isoformat()}",
                )
            )
            accepted += 1
            if accepted >= max(1, int(max_filings_per_ticker)):
                break

    if failures and queried:
        status = "partial"
    elif failures and not queried:
        status = "error"
    else:
        status = "ok"
    detail = f"{queried} company submissions queried; {len(filings)} supported recent filings"
    if failures:
        detail += "; " + "; ".join(failures[:8])
    return SecCollectionResult(status, detail, filings)
