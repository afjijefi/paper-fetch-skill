"""Install the final Markdown repair pass at the artifact-store boundary."""

from __future__ import annotations

from pathlib import Path

from .artifacts import ArtifactStore
from .markdown_repair import repair_markdown_file


def install_artifact_markdown_repair_hook() -> None:
    """Wrap ``ArtifactStore.write_text_file`` once for every Markdown output path."""

    current = ArtifactStore.write_text_file
    if getattr(current, "_paper_fetch_artifact_repair_hook", False):
        return

    def wrapped(
        self: ArtifactStore,
        path: Path,
        text: str,
        *,
        encoding: str = "utf-8",
        overwrite: bool = True,
        use_lock: bool = False,
    ) -> Path:
        saved = current(
            self,
            path,
            text,
            encoding=encoding,
            overwrite=overwrite,
            use_lock=use_lock,
        )
        if Path(saved).suffix.lower() == ".md":
            try:
                repair_markdown_file(saved)
            except Exception as exc:
                setattr(wrapped, "_paper_fetch_last_repair_error", str(exc))
        return saved

    setattr(wrapped, "_paper_fetch_artifact_repair_hook", True)
    ArtifactStore.write_text_file = wrapped


__all__ = ["install_artifact_markdown_repair_hook"]
