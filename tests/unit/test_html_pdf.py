from pathlib import Path

from paper_fetch.html_pdf import (
    HtmlPdfResult,
    build_print_html,
    detect_blocking_text,
)


def test_detects_challenge_without_bypassing_it():
    assert detect_blocking_text("Checking your browser before accessing the article") == "checking your browser"
    assert detect_blocking_text("Normal article text") == ""


def test_build_print_html_keeps_article_and_adds_base_and_print_css():
    rendered = build_print_html(
        {
            "baseUrl": "https://example.org/article/1",
            "title": "A <Scientific> Article",
            "styles": "<style>.equation{font-size:1em}</style>",
            "rootHtml": "<article><h1>Title</h1><figure><img src='figure.png'></figure></article>",
        }
    )
    assert '<base href="https://example.org/article/1">' in rendered
    assert "break-inside: avoid-page" in rendered
    assert "<article>" in rendered
    assert "A Scientific Article" in rendered


def test_result_is_json_serializable_contract(tmp_path: Path):
    result = HtmlPdfResult(
        status="success",
        pdf_path=str(tmp_path / "article.pdf"),
        source_url="https://example.org/article",
        page_count=4,
        pdf_bytes=12345,
    )
    payload = result.to_dict()
    assert payload["status"] == "success"
    assert payload["page_count"] == 4
