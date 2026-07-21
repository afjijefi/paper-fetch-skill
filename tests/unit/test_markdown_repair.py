from pathlib import Path

from paper_fetch.markdown_repair import (
    repair_latex_structure,
    repair_markdown_text,
    validate_latex_structure,
)


def _gif_fetcher(url: str, headers: dict[str, str], timeout: float):
    assert url.startswith("https://ieeexplore.ieee.org/mediastore/")
    assert headers.get("Referer") == "https://ieeexplore.ieee.org/"
    assert timeout > 0
    return b"GIF89a" + b"\x00" * 32, "image/gif"


def test_repairs_orphan_outer_group_before_matrix():
    source = r"{\begin{matrix} P_e = 1.5(v_d i_{gd}+v_q i_{gq}) \\ Q_e = 1.5(v_q i_{gd}-v_d i_{gq}) \end{matrix}"
    repaired, changed, validation = repair_latex_structure(source)
    assert changed is True
    assert repaired.startswith(r"\begin{matrix}")
    assert validate_latex_structure(repaired).valid is True
    assert validation.valid is True


def test_does_not_guess_at_internal_brace_damage():
    source = r"\frac{a}{b"
    repaired, changed, validation = repair_latex_structure(source)
    assert repaired == source
    assert changed is False
    assert validation.valid is False


def test_localizes_and_deduplicates_ieee_large_small_figure(tmp_path: Path):
    md_path = tmp_path / "paper.md"
    markdown = """Text before.

![Figure 1](https://ieeexplore.ieee.org/mediastore/IEEE/content/media/1/2/abc-large.gif)

![Figure 1](/mediastore/IEEE/content/media/1/2/abc-small.gif)

**Figure 1.** Caption.
"""
    repaired, report = repair_markdown_text(
        markdown,
        markdown_path=md_path,
        fetcher=_gif_fetcher,
    )
    assert report.localized_images == 1
    assert report.duplicate_images_removed == 1
    assert repaired.count("![Figure 1]") == 1
    assert "ieeexplore.ieee.org/mediastore" not in repaired
    assert "/mediastore/" not in repaired
    assert "paper_assets/abc-large.gif" in repaired
    assert (tmp_path / "paper_assets" / "abc-large.gif").is_file()


def test_repairs_display_formula_inside_markdown(tmp_path: Path):
    md_path = tmp_path / "paper.md"
    markdown = r"""**Equation 6.**

$$
{\begin{matrix} P_e = 1 \\ Q_e = 2 \end{matrix}
$$
"""
    repaired, report = repair_markdown_text(markdown, markdown_path=md_path)
    assert report.formulas_repaired == 1
    assert r"{\begin{matrix}" not in repaired
    assert r"\begin{matrix}" in repaired
    assert report.formulas_invalid == 0


def test_code_fence_is_not_modified(tmp_path: Path):
    md_path = tmp_path / "paper.md"
    markdown = """```latex
$$
{\\begin{matrix} a \\\\ b \\end{matrix}
$$
```
"""
    repaired, report = repair_markdown_text(markdown, markdown_path=md_path)
    assert repaired == markdown
    assert report.formulas_repaired == 0
