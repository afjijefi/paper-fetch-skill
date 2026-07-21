"""Public package surface for paper-fetch."""

from .markdown_repair import (
    MarkdownRepairReport,
    install_markdown_save_hook,
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
from .workflow import pipeline as _pipeline
from .workflow import rendering as _rendering

install_markdown_save_hook()
_pipeline.save_markdown_to_disk = _rendering.save_markdown_to_disk

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
