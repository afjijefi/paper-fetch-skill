from pathlib import Path

from paper_fetch.artifacts import ArtifactStore


def test_artifact_store_repairs_markdown_after_write(tmp_path: Path) -> None:
    target = tmp_path / "paper.md"
    source = r"""**Equation 6.**

$$
{\begin{matrix} P_e = 1 \\ Q_e = 2 \end{matrix}
$$
"""

    saved = ArtifactStore.from_download_dir(tmp_path).write_text_file(
        target,
        source,
        use_lock=True,
    )

    text = saved.read_text(encoding="utf-8")
    assert r"{\begin{matrix}" not in text
    assert r"\begin{matrix}" in text
