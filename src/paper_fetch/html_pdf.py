"""Render an article web page to a clean, MinerU-ready PDF.

The module intentionally does not bypass authentication, CAPTCHA, paywalls, or
publisher access controls. It reuses the caller's authorized browser profile,
waits for the visible article to finish rendering, creates a clean article-only
print page, and asks Chromium to generate a PDF.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Callable, Literal


_BLOCKING_TOKENS = (
    "captcha",
    "verify you are human",
    "verification required",
    "checking your browser",
    "access denied",
    "unusual traffic",
    "robot check",
    "enable javascript and cookies",
    "sign in to access",
    "institutional access required",
)
_ARTICLE_SELECTORS = (
    "article",
    "main article",
    "main",
    "[role='main']",
    ".article",
    ".article-content",
    ".article__body",
    ".article-body",
    ".c-article-body",
    ".document-main",
    "#article",
    "#main-content",
)
_EXPAND_TEXT_PATTERN = re.compile(
    r"^(show|view|read|load|expand|more|full text|references|show all|view all)",
    flags=re.IGNORECASE,
)


class HtmlPdfError(RuntimeError):
    """Base error for HTML-to-PDF rendering."""


class HtmlPdfActionRequired(HtmlPdfError):
    """Raised when the page visibly requires legal user interaction."""

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message)
        self.url = url


@dataclass(slots=True)
class HtmlPdfOptions:
    timeout_ms: int = 120_000
    headless: bool = True
    page_format: str = "A4"
    margin_mm: float = 14.0
    scale: float = 1.0
    viewport_width: int = 1440
    viewport_height: int = 1000
    print_background: bool = True
    minimum_text_chars: int = 800
    maximum_scroll_steps: int = 160


@dataclass(slots=True)
class HtmlPdfResult:
    status: Literal["success", "degraded", "action_required", "failed"]
    pdf_path: str
    source_url: str
    final_url: str = ""
    article_selector: str = ""
    page_count: int = 0
    pdf_bytes: int = 0
    html_text_chars: int = 0
    pdf_text_chars: int = 0
    figure_count: int = 0
    loaded_figure_count: int = 0
    formula_count: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_blocking_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).lower()
    return next((token for token in _BLOCKING_TOKENS if token in normalized), "")


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
            text_chars = sum(len(page.get_text("text") or "") for page in document)
            return int(getattr(document, "page_count", len(document))), text_chars
    except Exception:
        return 0, 0


def _input_url(value: str | Path) -> str:
    text = str(value)
    if text.startswith(("http://", "https://", "data:", "file:")):
        return text
    path = Path(text).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return path.as_uri()


def _prepare_original_page(page: Any, options: HtmlPdfOptions) -> dict[str, Any]:
    page.emulate_media(media="screen")
    try:
        page.evaluate("document.fonts && document.fonts.ready")
    except Exception:
        pass

    # Expand semantic disclosure elements and conservative text-labelled controls.
    page.evaluate(
        """
        () => {
          for (const details of document.querySelectorAll('details')) details.open = true;
          const pattern = /^(show|view|read|load|expand|more|full text|references|show all|view all)/i;
          for (const el of document.querySelectorAll('button, [role="button"]')) {
            const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
            if (pattern.test(text) && el.offsetParent !== null) {
              try { el.click(); } catch (_) {}
            }
          }
        }
        """
    )

    # Promote lazy-loaded and responsive images to the best visible candidate.
    page.evaluate(
        """
        () => {
          const chooseSrcset = value => {
            if (!value) return '';
            const parts = value.split(',').map(item => item.trim()).filter(Boolean);
            return parts.length ? parts[parts.length - 1].split(/\s+/)[0] : '';
          };
          for (const img of document.images) {
            const candidates = [
              img.getAttribute('data-full-src'), img.getAttribute('data-lg-src'),
              img.getAttribute('data-original'), img.getAttribute('data-src'),
              img.getAttribute('data-lazy-src'), chooseSrcset(img.getAttribute('srcset')),
              chooseSrcset(img.getAttribute('data-srcset')), img.currentSrc, img.src,
            ].filter(Boolean);
            if (candidates.length) img.src = candidates[0];
            img.loading = 'eager';
            img.decoding = 'sync';
          }
        }
        """
    )

    # Incremental scrolling triggers publisher lazy loaders without relying on networkidle.
    page.evaluate(
        """
        async ({steps}) => {
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          let previousHeight = 0;
          let stable = 0;
          for (let i = 0; i < steps; i++) {
            const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
            window.scrollTo(0, Math.min(height, i * 850));
            await sleep(80);
            if (height === previousHeight) stable += 1; else stable = 0;
            previousHeight = height;
            if (window.scrollY + window.innerHeight >= height - 8 && stable >= 3) break;
          }
          window.scrollTo(0, 0);
        }
        """,
        {"steps": options.maximum_scroll_steps},
    )

    # Wait for MathJax when the page exposes its normal completion promises.
    try:
        page.evaluate(
            """
            async () => {
              if (window.MathJax?.startup?.promise) await window.MathJax.startup.promise;
              if (window.MathJax?.typesetPromise) await window.MathJax.typesetPromise();
            }
            """
        )
    except Exception:
        pass

    try:
        page.wait_for_function(
            """
            () => [...document.images].every(img => !img.src || (img.complete && img.naturalWidth > 0))
            """,
            timeout=min(options.timeout_ms, 30_000),
        )
    except Exception:
        pass

    return page.evaluate(
        """
        selectors => {
          let root = null;
          let selector = '';
          for (const candidate of selectors) {
            const node = document.querySelector(candidate);
            if (node && (node.innerText || '').trim().length > 500) {
              root = node; selector = candidate; break;
            }
          }
          root ||= document.body;
          const images = [...root.querySelectorAll('img')];
          const formulaSelector = 'math, mjx-container, .MathJax, .equation, .formula, [data-equation], [class*="math"]';
          return {
            selector,
            baseUrl: document.baseURI,
            title: document.title || 'Article',
            rootHtml: root.outerHTML,
            styles: [...document.querySelectorAll('head style, head link[rel="stylesheet"]')]
              .map(node => node.outerHTML).join('\n'),
            textChars: (root.innerText || '').trim().length,
            figureCount: root.querySelectorAll('figure, .figure, .fig, img').length,
            loadedFigureCount: images.filter(img => img.complete && img.naturalWidth > 0).length,
            formulaCount: root.querySelectorAll(formulaSelector).length,
            bodyText: (document.body?.innerText || '').slice(0, 8000),
          };
        }
        """,
        list(_ARTICLE_SELECTORS),
    )


def build_print_html(snapshot: dict[str, Any]) -> str:
    base_url = str(snapshot.get("baseUrl") or "")
    title = re.sub(r"[<>]", "", str(snapshot.get("title") or "Article"))
    styles = str(snapshot.get("styles") or "")
    root_html = str(snapshot.get("rootHtml") or "")
    print_css = """
    <style id="paper-fetch-print-style">
      @page { size: A4; margin: 14mm; }
      html, body { background: #fff !important; color: #111 !important; }
      body { margin: 0 auto !important; max-width: 180mm !important;
             font-family: Arial, 'Times New Roman', serif !important;
             font-size: 10.5pt !important; line-height: 1.45 !important; }
      img, svg, canvas { max-width: 100% !important; height: auto !important; }
      figure, table, pre, blockquote, math, mjx-container, .equation, .formula {
        break-inside: avoid-page !important; page-break-inside: avoid !important;
      }
      table { width: 100% !important; border-collapse: collapse !important; }
      pre { white-space: pre-wrap !important; overflow-wrap: anywhere !important; }
      nav, aside, [role='navigation'], [aria-modal='true'], .cookie, .cookies,
      .advertisement, .ads, .social-share, .share-tools, .sticky, .floating,
      .toolbar, .recommendations, .related-content { display: none !important; }
      a { color: inherit !important; text-decoration: none !important; }
    </style>
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<base href={json.dumps(base_url)}><title>{title}</title>{styles}{print_css}"
        f"</head><body>{root_html}</body></html>"
    )


def render_article_to_pdf(
    source: str | Path,
    output_path: str | Path,
    *,
    options: HtmlPdfOptions | None = None,
    user_data_dir: str | Path | None = None,
    log: Callable[[str], None] = print,
) -> HtmlPdfResult:
    """Render a remote or local article page into a clean PDF.

    The caller is responsible for lawful access. Existing login state can be reused
    by providing ``user_data_dir``. A challenge or access gate returns
    ``action_required`` instead of attempting to bypass it.
    """

    active = options or HtmlPdfOptions()
    url = _input_url(source)
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    result = HtmlPdfResult(status="failed", pdf_path=str(target), source_url=url)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        result.error = f"Playwright unavailable: {exc}"
        return result

    context = None
    browser = None
    try:
        with sync_playwright() as playwright:
            launch_args = ["--disable-print-preview", "--disable-features=TranslateUI"]
            if user_data_dir:
                profile = Path(user_data_dir).expanduser().resolve()
                profile.mkdir(parents=True, exist_ok=True)
                context = playwright.chromium.launch_persistent_context(
                    str(profile),
                    headless=active.headless,
                    viewport={"width": active.viewport_width, "height": active.viewport_height},
                    accept_downloads=False,
                    args=launch_args,
                )
            else:
                browser = playwright.chromium.launch(headless=active.headless, args=launch_args)
                context = browser.new_context(
                    viewport={"width": active.viewport_width, "height": active.viewport_height}
                )

            page = context.pages[0] if context.pages else context.new_page()
            log(f"[HTML转PDF] 加载：{url}")
            response = page.goto(url, wait_until="domcontentloaded", timeout=active.timeout_ms)
            result.final_url = page.url
            if response is not None and response.status >= 400:
                result.warnings.append(f"HTTP {response.status}")
            snapshot = _prepare_original_page(page, active)
            blocker = detect_blocking_text(str(snapshot.get("bodyText") or ""))
            if blocker:
                result.status = "action_required"
                result.error = f"Page requires user interaction: {blocker}"
                return result

            result.article_selector = str(snapshot.get("selector") or "")
            result.html_text_chars = int(snapshot.get("textChars") or 0)
            result.figure_count = int(snapshot.get("figureCount") or 0)
            result.loaded_figure_count = int(snapshot.get("loadedFigureCount") or 0)
            result.formula_count = int(snapshot.get("formulaCount") or 0)
            if result.html_text_chars < active.minimum_text_chars:
                result.warnings.append("Article text is unexpectedly short before printing.")

            clean_page = context.new_page()
            clean_page.emulate_media(media="screen")
            clean_page.set_content(build_print_html(snapshot), wait_until="load", timeout=active.timeout_ms)
            try:
                clean_page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                pass
            clean_page.pdf(
                path=str(target),
                format=active.page_format,
                print_background=active.print_background,
                margin={
                    "top": f"{active.margin_mm}mm",
                    "bottom": f"{active.margin_mm}mm",
                    "left": f"{active.margin_mm}mm",
                    "right": f"{active.margin_mm}mm",
                },
                scale=active.scale,
                prefer_css_page_size=False,
                display_header_footer=False,
            )
            result.pdf_bytes = target.stat().st_size if target.exists() else 0
            result.page_count, result.pdf_text_chars = _pdf_metrics(target)
            if not target.exists() or result.pdf_bytes < 10_000 or result.page_count < 1:
                result.status = "failed"
                result.error = "Chromium did not produce a usable PDF."
                return result
            ratio_floor = min(active.minimum_text_chars, max(200, int(result.html_text_chars * 0.15)))
            if result.pdf_text_chars < ratio_floor:
                result.status = "degraded"
                result.warnings.append("PDF text layer is much shorter than the rendered article.")
            else:
                result.status = "success"
            return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paper-fetch-html-pdf")
    parser.add_argument("source", help="Article URL or local rendered HTML file")
    parser.add_argument("output", type=Path, help="Output PDF path")
    parser.add_argument("--user-data-dir", type=Path)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=120_000)
    args = parser.parse_args(argv)
    result = render_article_to_pdf(
        args.source,
        args.output,
        user_data_dir=args.user_data_dir,
        options=HtmlPdfOptions(headless=not args.headed, timeout_ms=args.timeout_ms),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status in {"success", "degraded"} else 2


__all__ = [
    "HtmlPdfActionRequired",
    "HtmlPdfError",
    "HtmlPdfOptions",
    "HtmlPdfResult",
    "build_print_html",
    "detect_blocking_text",
    "render_article_to_pdf",
]


if __name__ == "__main__":
    raise SystemExit(main())
