"""Selective IEEE backports from upstream 3.2 without changing browser backend.

This module is intentionally opt-in. Auto-Paper-Download imports and installs it
at startup so its existing bundled Chromium/CDP pool remains authoritative.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from collections.abc import Mapping

from ..reason_codes import ERROR, NO_RESULT
from ..runtime_browser import browser_context_options
from ..utils import normalize_text
from . import _ieee_html as ieee_html
from . import _ieee_metadata as ieee_metadata
from . import _ieee_url as ieee_url
from . import ieee
from .base import ProviderFailure

_URL_FIELDS = (
    "download_url",
    "source_url",
    "full_size_url",
    "url",
    "original_url",
    "preview_url",
    "figure_page_url",
    "path",
    "link",
)
_VARIANT_SUFFIX = re.compile(
    r"-(?:large|full|small|thumb|thumbnail|preview)(?=\.[a-z0-9]+$)",
    flags=re.IGNORECASE,
)


def html_browser_recovery_allowed(failure: ProviderFailure | None) -> bool:
    """Only retry browser-recoverable IEEE HTML failures."""

    if failure is None:
        return True
    if failure.code == "rate_limited":
        return False
    status_match = re.search(r"\bHTTP\s+(\d{3})\b", failure.message, re.IGNORECASE)
    if status_match is not None:
        return int(status_match.group(1)) in {401, 403}
    lowered = normalize_text(failure.message).lower()
    if any(
        token in lowered
        for token in ("rate limit", "too many requests", "http 429")
    ):
        return False
    if any(
        token in lowered
        for token in ("did not include #article", "empty #article shell")
    ):
        return True
    return failure.code in {ERROR, NO_RESULT}


def _media_family(url: str) -> str:
    value = normalize_text(url)
    if not value:
        return ""
    parsed = urlsplit(value)
    path = _VARIANT_SUFFIX.sub("", parsed.path.lower())
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _asset_quality(asset: Mapping[str, Any]) -> tuple[int, int, int, int]:
    urls = [normalize_text(str(asset.get(field) or "")) for field in _URL_FIELDS]
    has_large = any(
        re.search(r"-(?:large|full)\.[a-z0-9]+$", urlsplit(url).path, re.I)
        for url in urls
        if url
    )
    has_full_field = bool(normalize_text(str(asset.get("full_size_url") or "")))
    try:
        pixels = int(asset.get("width") or 0) * int(asset.get("height") or 0)
    except (TypeError, ValueError):
        pixels = 0
    try:
        downloaded = int(asset.get("downloaded_bytes") or 0)
    except (TypeError, ValueError):
        downloaded = 0
    return (int(has_full_field), int(has_large), pixels, downloaded)


def _browser_landing_attempt(
    client: Any,
    doi: str,
    metadata: Mapping[str, Any],
    direct_failure: ProviderFailure,
) -> ieee_metadata.IeeeLandingAttempt:
    runtime_context = getattr(client, "_ieee_backport_runtime_context", None)
    if runtime_context is None:
        raise direct_failure
    normalized_doi = ieee.normalize_doi(doi)
    landing_url = client._resolve_landing_url(normalized_doi, metadata)
    browser_context = page = None
    try:
        options = browser_context_options(
            user_agent=client.browser_user_agent,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        browser_context = runtime_context.new_playwright_context(
            headless=True, **options
        )
        page = browser_context.new_page()
        with contextlib.suppress(Exception):
            page.goto(landing_url, wait_until="commit", timeout=60_000)
        page.wait_for_selector("body", state="attached", timeout=20_000)
        with contextlib.suppress(Exception):
            page.wait_for_selector(
                "#article, meta[name='citation_title'], script",
                state="attached",
                timeout=8_000,
            )
        html_text = str(page.content() or "")
        response_url = (
            normalize_text(str(getattr(page, "url", "") or "")) or landing_url
        )
        landing_metadata = ieee_metadata._parse_landing_metadata(html_text)
        article_number = (
            ieee_url._article_number_from_metadata(landing_metadata)
            or ieee_url._article_number_from_url(response_url)
            or ieee_url._article_number_from_metadata(metadata)
            or ieee_url._article_number_from_url(landing_url)
        )
        if not article_number:
            raise ProviderFailure(
                NO_RESULT,
                "IEEE browser landing recovery did not expose an article number.",
            )
        merged_metadata = ieee_metadata._merge_ieee_metadata(
            metadata, landing_metadata, response_url
        )
        if not merged_metadata.get("doi"):
            merged_metadata["doi"] = normalized_doi
        merged_metadata["article_number"] = article_number
        merged_metadata["articleNumber"] = article_number
        return ieee_metadata.IeeeLandingAttempt(
            normalized_doi=normalized_doi,
            landing_url=landing_url,
            response_url=response_url,
            html_text=html_text,
            merged_metadata=merged_metadata,
            article_number=article_number,
            landing_metadata=landing_metadata,
        )
    except ProviderFailure:
        raise
    except Exception as exc:
        message = normalize_text(str(exc)) or exc.__class__.__name__
        raise ProviderFailure(
            ERROR,
            "IEEE landing retrieval failed through direct HTTP and shared-browser "
            f"recovery ({direct_failure.message}; {message}).",
        ) from exc
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                page.close()
        if browser_context is not None:
            with contextlib.suppress(Exception):
                browser_context.close()


def install() -> None:
    if getattr(ieee.IeeeClient, "_auto_paper_ieee_v316", False):
        return

    original_identity_values = ieee_html._ieee_asset_identity_values
    original_fetch_raw = ieee.IeeeClient.fetch_raw_fulltext
    original_fetch_landing = ieee.IeeeClient._fetch_landing_attempt
    original_fetch_browser_html = ieee.IeeeClient._fetch_browser_html_payload

    def identity_values(asset: Mapping[str, Any]) -> list[str]:
        values = list(original_identity_values(asset))
        for field in _URL_FIELDS:
            family = _media_family(str(asset.get(field) or ""))
            if family:
                marker = "ieee-media-family:" + family
                if marker not in values:
                    values.append(marker)
        return values

    def select_survivor(
        candidates: list[dict[str, Any]], current_assets: list[dict[str, Any]]
    ) -> dict[str, Any]:
        current_order = {
            id(asset): index for index, asset in enumerate(current_assets)
        }
        fallback_order = len(current_assets)
        return max(
            candidates,
            key=lambda asset: (
                ieee_html._ieee_asset_priority(asset),
                _asset_quality(asset),
                -current_order.get(id(asset), fallback_order),
            ),
        )

    def fetch_raw_fulltext(
        self: Any, doi: str, metadata: Any, *, context: Any = None
    ) -> Any:
        self._ieee_backport_runtime_context = self._runtime_context(context)
        try:
            return original_fetch_raw(self, doi, metadata, context=context)
        finally:
            self._ieee_backport_runtime_context = None

    def fetch_landing_attempt(
        self: Any, doi: str, metadata: Mapping[str, Any]
    ) -> ieee_metadata.IeeeLandingAttempt:
        try:
            return original_fetch_landing(self, doi, metadata)
        except ProviderFailure as failure:
            if not html_browser_recovery_allowed(failure):
                raise
            return _browser_landing_attempt(self, doi, metadata, failure)

    def fetch_browser_html_payload(
        self: Any,
        landing_attempt: ieee_metadata.IeeeLandingAttempt,
        *,
        direct_html_failure: ProviderFailure | None,
        context: Any,
    ) -> Any:
        if not html_browser_recovery_allowed(direct_html_failure):
            raise direct_html_failure or ProviderFailure(
                NO_RESULT, "IEEE browser HTML recovery was not eligible."
            )
        return original_fetch_browser_html(
            self,
            landing_attempt,
            direct_html_failure=direct_html_failure,
            context=context,
        )

    ieee_html._ieee_asset_identity_values = identity_values
    ieee_html._select_ieee_asset_survivor = select_survivor
    ieee.IeeeClient.fetch_raw_fulltext = fetch_raw_fulltext
    ieee.IeeeClient._fetch_landing_attempt = fetch_landing_attempt
    ieee.IeeeClient._fetch_browser_html_payload = fetch_browser_html_payload
    ieee.IeeeClient._auto_paper_ieee_v316 = True


__all__ = ["html_browser_recovery_allowed", "install"]
