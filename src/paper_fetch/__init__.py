"""Public package surface for paper-fetch."""

from .article_pdf import (
    ArticlePdfResult,
    obtain_article_pdf,
    official_pdf_candidates_from_html,
)
from .artifact_repair_hook import install_artifact_markdown_repair_hook
from .html_pdf import (
    HtmlPdfActionRequired,
    HtmlPdfError,
    HtmlPdfOptions,
    HtmlPdfResult,
    render_article_to_pdf,
)
from .markdown_repair import (
    MarkdownRepairReport,
    repair_latex_structure,
    repair_markdown_file,
    repair_markdown_text,
    validate_latex_structure,
)
from .models import (
    ArticleModel,
    FetchEnvelope,
    Metadata,
    Quality,
    RenderOptions,
    Section,
    TokenEstimateBreakdown,
)
from .service import FetchStrategy, PaperFetchFailure, fetch_paper, resolve_paper

install_artifact_markdown_repair_hook()

__all__ = [
    "ArticleModel",
    "ArticlePdfResult",
    "FetchEnvelope",
    "FetchStrategy",
    "HtmlPdfActionRequired",
    "HtmlPdfError",
    "HtmlPdfOptions",
    "HtmlPdfResult",
    "MarkdownRepairReport",
    "Metadata",
    "PaperFetchFailure",
    "Quality",
    "RenderOptions",
    "Section",
    "TokenEstimateBreakdown",
    "fetch_paper",
    "obtain_article_pdf",
    "official_pdf_candidates_from_html",
    "render_article_to_pdf",
    "repair_latex_structure",
    "repair_markdown_file",
    "repair_markdown_text",
    "resolve_paper",
    "validate_latex_structure",
]
