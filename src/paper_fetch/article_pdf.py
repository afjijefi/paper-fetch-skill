"""Obtain a MinerU-ready article PDF, preferring the publisher's official PDF.

The workflow is conservative and authorization-aware:

1. Open the article page with the caller's existing Playwright profile.
2. Read publisher-declared PDF links from metadata and visible article links.
3. Download only a response that validates as a real PDF.
4. If no official PDF is available, delegate to the clean HTML-print renderer.

No CAPTCHA, paywall, entitlement, or authentication control is bypassed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any, Callable, Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .html_pdf import HtmlPdfOptions, detect_blocking_text, render_article_to_pdf


@dataclass(slots=True)
class ArticlePdfResult:
    status: Literal["success", "degraded", "action_required", "failed"]
    pdf_path: str
    source_url: str
    source_kind: Literal["official_pdf", "html_print", ""] = ""
    final_url: str = ""
    official_pdf_url: str = ""
    page_count: int = 0
    pdf_bytes: int = 0
    pdf_text_chars: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def official_pdf_candidates_from_html(html: str, base_url: str) -> list[str]:
    """Return stable, ordered publisher PDF candidates declared by a page."""

    soup = BeautifulSoup(str(html or ""), "html.parser")
    candidates: list[str] = []

    def add(value: str | None) -> None:
        text = str(value or "").strip()
        if not text:
            return
        absolute = urljoin(base_url, text)
        if not absolute.startswith(("http://", "https://")):
            return
        key = absolute.lower()
        if key not in {item.lower() for item in candidates}:
            candidates.append(absolute)

    for name in (
        "citation_pdf_url",
        "wkhealth_pdf_url",
        "dc.identifier.pdf",
        "eprints.document_url",
    ):
        node = soup.find("meta", attrs={"name": re.compile(f"^{re.escape(name)}$", re.I)})
        if node is not None:
            add(node.get("content"))

    for node in soup.select(
        "link[type='application/pdf'][href], "
        "a[type='application/pdf'][href], "
        "a[data-pdf-url][href], "
        "a[href*='/pdf/'], a[href*='downloadpdf'], a[href$='.pdf']"
    ):
        add(node.get("data-pdf-url") or node.get("href"))
    return candidates[:12]


def _pdf_metrics(path: Path) -> tuple[int, int]:
    try:
        import pymupdf
    except Exception:
        try:
            import fitz as pymupdf
        except Exception:
            return 0, 0
    try:
        with pymupdf.open(str(path)) as document:
            return int(document.page_count), sum(
                len(page.get_text("text") or "") for page in document
            )
    except Exception:
        return 0, 0


def _looks_like_pdf(payload: bytes, content_type: str, url: str) -> bool:
    return (
        payload.startswith(b"%PDF-")
        or "application/pdf" in str(content_type or "").lower()
        or str(url or "").lower().split("?", 1)[0].endswith(".pdf")
    ) and len(payload) >= 1024


def obtain_article_pdf(
    source_url: str,
    output_path: str | Path,
    *,
    options: HtmlPdfOptions | None = None,
    user_data_dir: str | Path | None = None,
    log: Callable[[str], None] = print,
) -> ArticlePdfResult:
    """Prefer an official publisher PDF, then fall back to clean HTML printing."""

    active = options or HtmlPdfOptions()
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    result = ArticlePdfResult(
        status="failed",
        pdf_path=str(target),
        source_url=str(source_url),
    )

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        result.error = f"Playwright unavailable: {exc}"
        return result

    context = None
    browser = None
    try:
        with sync_playwright() as playwright:
            if user_data_dir:
                profile = Path(user_data_dir).expanduser().resolve()
                profile.mkdir(parents=True, exist_ok=True)
                context = playwright.chromium.launch_persistent_context(
                    str(profile),
                    headless=active.headless,
                    viewport={
                        "width": active.viewport_width,
                        "height": active.viewport_height,
                    },
                )
            else:
                browser = playwright.chromium.launch(headless=active.headless)
                context = browser.new_context(
                    viewport={
                        "width": active.viewport_width,
                        "height": active.viewport_height,
                    }
                )

            page = context.pages[0] if context.pages else context.new_page()
            log(f"[论文PDF] 检查出版社官方PDF：{source_url}")
            page.goto(
                str(source_url),
                wait_until="domcontentloaded",
                timeout=active.timeout_ms,
            )
            result.final_url = page.url
            body_text = page.locator("body").inner_text(timeout=min(active.timeout_ms, 30_000))
            blocker = detect_blocking_text(body_text[:8000])
            if blocker:
                result.status = "action_required"
                result.error = f"Page requires user interaction: {blocker}"
                return result

            html = page.content()
            candidates = official_pdf_candidates_from_html(html, page.url)
            for candidate in candidates:
                try:
                    response = context.request.get(
                        candidate,
                        headers={"Referer": page.url, "Accept": "application/pdf,*/*;q=0.8"},
                        timeout=active.timeout_ms,
                    )
                    body = response.body()
                    content_type = response.headers.get("content-type", "")
                    final_url = response.url
                except Exception as exc:
                    result.warnings.append(
                        f"Official PDF request failed for {candidate}: {type(exc).__name__}"
                    )
                    continue
                if not _looks_like_pdf(body, content_type, final_url):
                    result.warnings.append(
                        f"Official PDF candidate was not a PDF: {candidate}"
                    )
                    continue
                target.write_bytes(body)
                result.source_kind = "official_pdf"
                result.official_pdf_url = candidate
                result.final_url = final_url
                result.pdf_bytes = len(body)
                result.page_count, result.pdf_text_chars = _pdf_metrics(target)
                result.status = (
                    "success"
                    if result.page_count > 0 and result.pdf_bytes >= 10_000
                    else "degraded"
                )
                log(f"[论文PDF] 已获得官方PDF：{candidate}")
                return result
    except Exception as exc:
        result.warnings.append(
            f"Official PDF discovery failed: {type(exc).__name__}: {exc}"
        )
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass

    printed = render_article_to_pdf(
        source_url,
        target,
        options=active,
        user_data_dir=user_data_dir,
        log=log,
    )
    result.status = printed.status
    result.source_kind = "html_print" if printed.status in {"success", "degraded"} else ""
    result.final_url = printed.final_url
    result.page_count = printed.page_count
    result.pdf_bytes = printed.pdf_bytes
    result.pdf_text_chars = printed.pdf_text_chars
    result.warnings.extend(printed.warnings)
    result.error = printed.error
    return result


__all__ = [
    "ArticlePdfResult",
    "obtain_article_pdf",
    "official_pdf_candidates_from_html",
]
