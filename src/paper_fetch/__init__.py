"""Public package surface for paper-fetch."""

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
from .markdown_repair import (
    MarkdownRepairReport,
    install_markdown_save_hook,
    repair_latex_structure,
    repair_markdown_file,
    repair_markdown_text,
    validate_latex_structure,
)

# Install after the normal service imports complete. CLI imports of
# workflow.rendering.save_markdown_to_disk then receive the wrapped saver.
install_markdown_save_hook()

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
