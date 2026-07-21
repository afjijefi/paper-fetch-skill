"""Public package surface for paper-fetch."""

from .artifact_repair_hook import install_artifact_markdown_repair_hook
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
    "FetchEnvelope",
    "FetchStrategy",
    "MarkdownRepairReport",
    "Metadata",
    "PaperFetchFailure",
    "Quality",
    "RenderOptions",
    "Section",
    "TokenEstimateBreakdown",
    "fetch_paper",
    "repair_latex_structure",
    "repair_markdown_file",
    "repair_markdown_text",
    "resolve_paper",
    "validate_latex_structure",
]
