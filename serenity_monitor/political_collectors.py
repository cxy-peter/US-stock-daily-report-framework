"""Public-source collectors for political communication research.

The collectors use documented/public HTTP endpoints only.  They do not bypass
logins, browser challenges, robots controls or paywalls.  Each collector returns
explicit source health and raw communication documents for the separate policy
claim model.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urljoin, urlparse

import requests

from .political_communications import CommunicationDocument


_ALLOWED_WHITEHOUSE_HOSTS = frozenset({"whitehouse.gov", "www.whitehouse.gov"})
_ALLOWED_X_HOST = "api.x.com"
_DEFAULT_USER_AGENT = "serenity-political-research/1.0 (+owner research; no trading)"


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    status: str
    observed_at: str
    detail: str
    item_count: int

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class CollectionResult:
    documents: tuple[CommunicationDocument, ...]
    source_health: tuple[SourceHealth, ...]


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        attributes = dict(attrs)
        self._href = attributes.get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            self.links.append((self._href, text))
            self._href = None
            self._text = []


class _ArticleParser(HTMLParser):
    """Conservative text/meta extractor for public article pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.published: str | None = None
        self.author = ""
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._capture_title = False
        self._skip_depth = 0
        self._article_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {str(key).casefold(): value for key, value in attrs}
        if tag in {"script", "style", "nav", "footer", "form", "svg", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._capture_title = True
        if tag in {"article", "main"}:
            self._article_depth += 1
        if tag == "meta":
            name = str(attributes.get("property") or attributes.get("name") or "").casefold()
            content = str(attributes.get("content") or "").strip()
            if name in {"og:title", "twitter:title"} and content:
                self.title = content
            elif name in {"article:published_time", "date", "datepublished"} and content:
                self.published = content
            elif name in {"author", "article:author"} and content:
                self.author = content
        if tag == "time" and attributes.get("datetime"):
            self.published = str(attributes["datetime"])

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "nav", "footer", "form", "svg", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._capture_title = False
        if tag in {"article", "main"} and self._article_depth:
            self._article_depth -= 1
        if tag in {"p", "li", "h1", "h2", "h3", "blockquote"} and self._text_parts:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._capture_title:
            self._title_parts.append(data)
        if self._article_depth:
            text = re.sub(r"\s+", " ", data).strip()
            if text:
                self._text_parts.append(text)

    def result(self) -> tuple[str, str, str | None, str]:
        title = self.title or re.sub(r"\s+", " ", " ".join(self._title_parts)).strip()
        body = re.sub(r"[ \t]+", " ", " ".join(self._text_parts))
        body = re.sub(r"\s*\n\s*", "\n", body).strip()
        return unescape(title), unescape(body), self.published, self.author


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _rfc3339(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_date(value: str | None, fallback: dt.datetime) -> dt.datetime:
    if not value:
        return fallback
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return fallback
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _safe_public_url(url: str, allowed_hosts: Iterable[str] | None = None) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("source URL must be HTTPS")
    if allowed_hosts is not None and parsed.hostname.casefold() not in {item.casefold() for item in allowed_hosts}:
        raise ValueError("source host is not allowed")
    return url


def _get(
    session: requests.Session,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20.0,
) -> requests.Response:
    merged_headers = {"User-Agent": _DEFAULT_USER_AGENT, "Accept": "text/html,application/json,application/rss+xml,application/xml"}
    if headers:
        merged_headers.update(headers)
    response = session.get(url, params=params, headers=merged_headers, timeout=timeout)
    response.raise_for_status()
    if len(response.content) > 20_000_000:
        raise ValueError("source response exceeds size limit")
    return response


class WhiteHouseCollector:
    """Collect direct White House actions, statements, releases and remarks."""

    DEFAULT_LISTINGS = (
        "https://www.whitehouse.gov/presidential-actions/",
        "https://www.whitehouse.gov/briefings-statements/",
        "https://www.whitehouse.gov/releases/",
        "https://www.whitehouse.gov/fact-sheets/",
    )

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        clock=_utc_now,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock = clock

    def _article_links(self, listing_url: str, html: str, limit: int) -> list[str]:
        parser = _LinkParser()
        parser.feed(html)
        links: list[str] = []
        for href, _text in parser.links:
            candidate = urljoin(listing_url, href)
            parsed = urlparse(candidate)
            if parsed.hostname not in _ALLOWED_WHITEHOUSE_HOSTS:
                continue
            if not any(
                marker in parsed.path
                for marker in (
                    "/presidential-actions/",
                    "/briefings-statements/",
                    "/releases/",
                    "/fact-sheets/",
                )
            ):
                continue
            if parsed.path.rstrip("/") == urlparse(listing_url).path.rstrip("/"):
                continue
            candidate = f"https://www.whitehouse.gov{parsed.path}"
            if candidate not in links:
                links.append(candidate)
            if len(links) >= limit:
                break
        return links

    @staticmethod
    def _source_type(url: str, body: str) -> str:
        path = urlparse(url).path
        lowered = body.casefold()
        if "/presidential-actions/" in path:
            if "executive order" in lowered or "signed" in lowered:
                return "signed_official_action"
            return "official_order"
        if "/fact-sheets/" in path:
            return "official_fact_sheet"
        if "press secretary" in lowered or "briefing" in lowered:
            return "official_press_briefing"
        return "official_speech"

    def collect(
        self,
        *,
        actor_id: str = "white_house",
        listing_urls: Sequence[str] | None = None,
        per_listing_limit: int = 8,
        since: dt.datetime | None = None,
    ) -> CollectionResult:
        now = self.clock().astimezone(dt.timezone.utc)
        since = since or (now - dt.timedelta(days=7))
        documents: list[CommunicationDocument] = []
        health: list[SourceHealth] = []
        for listing_url in listing_urls or self.DEFAULT_LISTINGS:
            source_id = f"whitehouse:{urlparse(listing_url).path.strip('/').replace('/', '_') or 'home'}"
            try:
                _safe_public_url(listing_url, _ALLOWED_WHITEHOUSE_HOSTS)
                listing = _get(self.session, listing_url, timeout=self.timeout)
                links = self._article_links(listing_url, listing.text, per_listing_limit)
                accepted = 0
                for url in links:
                    try:
                        article_response = _get(self.session, url, timeout=self.timeout)
                        parser = _ArticleParser()
                        parser.feed(article_response.text)
                        title, body, published, author = parser.result()
                        observed_at = _parse_date(published, now)
                        if observed_at < since or not body:
                            continue
                        documents.append(
                            CommunicationDocument(
                                document_id=hashlib.sha256(url.encode()).hexdigest(),
                                actor_id=actor_id,
                                observed_at=observed_at,
                                source_type=self._source_type(url, body),
                                title=title,
                                body=body,
                                source_url=url,
                                outlet=author or "The White House",
                                direct_quote=True,
                            )
                        )
                        accepted += 1
                    except (requests.RequestException, ValueError, UnicodeError):
                        continue
                health.append(
                    SourceHealth(source_id, "healthy" if accepted else "partial", _rfc3339(now), "public White House listing/article retrieval", accepted)
                )
            except (requests.RequestException, ValueError, UnicodeError) as exc:
                health.append(SourceHealth(source_id, "error", _rfc3339(now), type(exc).__name__, 0))
        unique = {item.document_id: item for item in documents}
        return CollectionResult(tuple(sorted(unique.values(), key=lambda item: item.observed_at, reverse=True)), tuple(health))


class XOfficialCollector:
    """Collect recent posts from configured official X accounts using API v2."""

    def __init__(
        self,
        *,
        bearer_token: str,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        clock=_utc_now,
    ) -> None:
        if not str(bearer_token).strip():
            raise ValueError("bearer_token is required")
        self._token = str(bearer_token).strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock = clock

    def __repr__(self) -> str:
        return "XOfficialCollector(bearer_token=<redacted>)"

    @property
    def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self._token}", "User-Agent": _DEFAULT_USER_AGENT}

    def collect(
        self,
        handles: Mapping[str, str],
        *,
        max_results: int = 20,
        start_time: dt.datetime | None = None,
    ) -> CollectionResult:
        now = self.clock().astimezone(dt.timezone.utc)
        documents: list[CommunicationDocument] = []
        health: list[SourceHealth] = []
        start_time = start_time or (now - dt.timedelta(days=7))
        for actor_id, raw_handle in handles.items():
            handle = str(raw_handle).strip().lstrip("@")
            source_id = f"x:{handle.casefold()}"
            try:
                user_response = _get(
                    self.session,
                    f"https://{_ALLOWED_X_HOST}/2/users/by/username/{handle}",
                    headers=self.headers,
                    timeout=self.timeout,
                )
                user_data = user_response.json().get("data") or {}
                user_id = str(user_data.get("id") or "")
                if not user_id:
                    raise ValueError("x user id missing")
                params = {
                    "max_results": max(5, min(int(max_results), 100)),
                    "start_time": _rfc3339(start_time),
                    "exclude": "replies,retweets",
                    "tweet.fields": "created_at,public_metrics,lang,entities,referenced_tweets",
                }
                timeline = _get(
                    self.session,
                    f"https://{_ALLOWED_X_HOST}/2/users/{user_id}/tweets",
                    params=params,
                    headers=self.headers,
                    timeout=self.timeout,
                ).json()
                accepted = 0
                for row in timeline.get("data") or []:
                    text = str(row.get("text") or "").strip()
                    if not text:
                        continue
                    metrics = row.get("public_metrics") or {}
                    engagement = sum(float(metrics.get(key) or 0.0) for key in ("like_count", "retweet_count", "reply_count", "quote_count"))
                    tweet_id = str(row.get("id") or "")
                    documents.append(
                        CommunicationDocument(
                            document_id=f"x:{tweet_id}",
                            actor_id=str(actor_id).casefold(),
                            observed_at=_parse_date(row.get("created_at"), now),
                            source_type="official_x",
                            title=f"@{handle} post",
                            body=text,
                            source_url=f"https://x.com/{handle}/status/{tweet_id}",
                            outlet="X",
                            engagement=engagement,
                            direct_quote=True,
                        )
                    )
                    accepted += 1
                health.append(SourceHealth(source_id, "healthy", _rfc3339(now), "official X API v2 timeline", accepted))
            except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
                health.append(SourceHealth(source_id, "error", _rfc3339(now), type(exc).__name__, 0))
        return CollectionResult(tuple(sorted(documents, key=lambda item: item.observed_at, reverse=True)), tuple(health))


class RssMediaCollector:
    """Collect public RSS/Atom items for direct quotes and media assessments."""

    def __init__(self, *, session: requests.Session | None = None, timeout: float = 20.0, clock=_utc_now) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock = clock

    @staticmethod
    def _text(element: ET.Element | None, *names: str) -> str:
        if element is None:
            return ""
        for name in names:
            child = element.find(name)
            if child is not None and child.text:
                return child.text.strip()
        return ""

    def collect(
        self,
        feeds: Sequence[Mapping[str, Any]],
        *,
        since: dt.datetime | None = None,
        per_feed_limit: int = 30,
    ) -> CollectionResult:
        now = self.clock().astimezone(dt.timezone.utc)
        since = since or (now - dt.timedelta(days=3))
        documents: list[CommunicationDocument] = []
        health: list[SourceHealth] = []
        for feed in feeds:
            url = str(feed.get("url") or "").strip()
            source_id = str(feed.get("source_id") or urlparse(url).hostname or "rss")
            actor_id = str(feed.get("actor_id") or "media").casefold()
            source_type = str(feed.get("source_type") or "media_analysis").casefold()
            outlet = str(feed.get("outlet") or source_id)
            try:
                _safe_public_url(url)
                response = _get(self.session, url, timeout=self.timeout)
                root = ET.fromstring(response.text)
                entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
                accepted = 0
                for entry in entries[: max(1, per_feed_limit)]:
                    title = self._text(entry, "title", "{http://www.w3.org/2005/Atom}title")
                    description = self._text(entry, "description", "summary", "{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content")
                    link = self._text(entry, "link")
                    if not link:
                        atom_link = entry.find("{http://www.w3.org/2005/Atom}link")
                        link = "" if atom_link is None else str(atom_link.attrib.get("href") or "")
                    published = self._text(entry, "pubDate", "published", "updated", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated")
                    observed_at = _parse_date(published, now)
                    if observed_at < since:
                        continue
                    body = re.sub(r"<[^>]+>", " ", unescape(description))
                    body = re.sub(r"\s+", " ", body).strip()
                    if not title and not body:
                        continue
                    material = link or f"{source_id}|{published}|{title}"
                    documents.append(
                        CommunicationDocument(
                            document_id=hashlib.sha256(material.encode()).hexdigest(),
                            actor_id=actor_id,
                            observed_at=observed_at,
                            source_type=source_type,
                            title=title,
                            body=f"{title}. {body}".strip(),
                            source_url=link,
                            outlet=outlet,
                            direct_quote=source_type == "media_direct_quote",
                        )
                    )
                    accepted += 1
                health.append(SourceHealth(source_id, "healthy" if accepted else "partial", _rfc3339(now), "public RSS/Atom retrieval", accepted))
            except (requests.RequestException, ValueError, ET.ParseError, UnicodeError) as exc:
                health.append(SourceHealth(source_id, "error", _rfc3339(now), type(exc).__name__, 0))
        return CollectionResult(tuple(sorted(documents, key=lambda item: item.observed_at, reverse=True)), tuple(health))


__all__ = [
    "CollectionResult",
    "RssMediaCollector",
    "SourceHealth",
    "WhiteHouseCollector",
    "XOfficialCollector",
]
