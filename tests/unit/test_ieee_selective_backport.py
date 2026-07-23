from __future__ import annotations

from paper_fetch.providers import _ieee_html as ieee_html
from paper_fetch.providers.base import ProviderFailure
from paper_fetch.providers.ieee_selective_backport import (
    html_browser_recovery_allowed,
    install,
)


def test_ieee_large_and_preview_variants_dedupe_to_large() -> None:
    install()
    assets = [
        {
            "kind": "figure",
            "url": "https://ieeexplore.ieee.org/mediastore/123-preview.png",
            "preview_url": "https://ieeexplore.ieee.org/mediastore/123-preview.png",
            "caption": "Figure 1",
        },
        {
            "kind": "figure",
            "url": "https://ieeexplore.ieee.org/mediastore/123-large.png",
            "full_size_url": "https://ieeexplore.ieee.org/mediastore/123-large.png",
            "caption": "Figure 1",
        },
    ]
    deduped = ieee_html._dedupe_ieee_assets_by_priority(
        assets, merge_fields=ieee_html.IEEE_ASSET_URL_FIELDS
    )
    assert len(deduped) == 1
    assert deduped[0]["full_size_url"].endswith("123-large.png")
    assert deduped[0]["preview_url"].endswith("123-preview.png")


def test_ieee_browser_recovery_skips_rate_limit_and_nonrecoverable_status() -> None:
    assert not html_browser_recovery_allowed(
        ProviderFailure("rate_limited", "HTTP 429 Too Many Requests")
    )
    assert not html_browser_recovery_allowed(
        ProviderFailure("error", "HTTP 404 Not Found")
    )
    assert html_browser_recovery_allowed(
        ProviderFailure("error", "HTTP 403 Forbidden")
    )
    assert html_browser_recovery_allowed(
        ProviderFailure("no_result", "IEEE dynamic HTML endpoint did not include #article.")
    )
