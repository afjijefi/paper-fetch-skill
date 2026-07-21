"""Post-render Markdown quality repair for local archival outputs.

The repair pass is deliberately conservative. It runs only on Markdown files
that have already been written by the normal paper-fetch pipeline and fixes two
classes of output defects:

* duplicate/remote body images that should live in the sibling ``*_assets``
  directory; and
* display-math blocks with a structurally invalid but safely repairable outer
  group, such as ``{\\begin{matrix} ... \\end{matrix}``.

The module uses only the Python standard library so it is safe in offline and
frozen distributions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import mimetypes
from pathlib import Path
import re
from typing import Callable
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<dest><[^>]+>|[^)\s]+)(?:\s+[\"'](?P<title>[^\"']*)[\"'])?\)",
    flags=re.IGNORECASE,
)
_DISPLAY_MATH_PATTERN = re.compile(r"\$\$(?P<body>.*?)\$\$", flags=re.DOTALL)
_ENV_TOKEN_PATTERN = re.compile(r"\\(?P<kind>begin|end)\s*\{(?P<name>[^{}]+)\}")
_SIZE_SUFFIX_PATTERN = re.compile(
    r"-(?:large|full|small|thumb|thumbnail|preview)(?=\.[A-Za-z0-9]+$)",
    flags=re.IGNORECASE,
)
_CODE_FENCE_PATTERN = re.compile(r"(^|\n)(?P<fence>`{3,}|~{3,}).*?\n.*?\n(?P=fence)(?=\n|$)", flags=re.DOTALL)
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff"}


@dataclass(slots=True)
class MarkdownRepairReport:
    path: str
    changed: bool = False
    localized_images: int = 0
    reused_local_images: int = 0
    duplicate_images_removed: int = 0
    remote_images_unresolved: int = 0
    formulas_repaired: int = 0
    formulas_invalid: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LatexValidation:
    valid: bool
    reason: str = ""


ImageFetcher = Callable[[str, dict[str, str], float], tuple[bytes, str]]


def _strip_destination(value: str) -> str:
    text = str(value or "").strip()
    return text[1:-1].strip() if text.startswith("<") and text.endswith(">") else text


def _absolute_remote_url(value: str) -> str | None:
    destination = _strip_destination(value)
    if destination.startswith("//"):
        return "https:" + destination
    if destination.startswith(("http://", "https://")):
        return destination
    if destination.startswith("/mediastore/"):
        return "https://ieeexplore.ieee.org" + destination
    return None


def _image_identity(alt: str, destination: str) -> str:
    label = re.sub(r"\s+", " ", str(alt or "").strip().lower())
    remote = _absolute_remote_url(destination)
    parsed = urlparse(remote or _strip_destination(destination))
    basename = unquote(Path(parsed.path).name).lower()
    basename = _SIZE_SUFFIX_PATTERN.sub("", basename)
    if label:
        return f"{label}|{basename}"
    return basename or hashlib.sha1(destination.encode("utf-8", errors="replace")).hexdigest()


def _candidate_priority(destination: str) -> tuple[int, int]:
    text = _strip_destination(destination).lower()
    if re.search(r"-(?:large|full)(?=\.[a-z0-9]+(?:[?#]|$))", text):
        return (0, len(text))
    if re.search(r"-(?:small|thumb|thumbnail|preview)(?=\.[a-z0-9]+(?:[?#]|$))", text):
        return (2, len(text))
    return (1, len(text))


def _safe_stem(value: str, fallback: str = "image") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    return (text or fallback)[:120]


def _guess_extension(url: str, content_type: str) -> str:
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return suffix
    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_extension(mime) or ""
    return ".jpg" if guessed == ".jpe" else (guessed if guessed in _IMAGE_EXTENSIONS else ".img")


def _looks_like_image(payload: bytes, content_type: str) -> bool:
    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return True
    prefixes = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF", b"BM")
    return any(payload.startswith(prefix) for prefix in prefixes) or payload.lstrip().startswith(b"<svg")


def _default_fetcher(url: str, headers: dict[str, str], timeout: float) -> tuple[bytes, str]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - caller controls archival URLs
        body = response.read()
        content_type = str(response.headers.get("Content-Type") or "")
    return body, content_type


def _existing_asset_for_url(asset_dir: Path, destination: str) -> Path | None:
    if not asset_dir.is_dir():
        return None
    remote = _absolute_remote_url(destination)
    parsed = urlparse(remote or _strip_destination(destination))
    basename = Path(unquote(parsed.path)).name
    normalized = _SIZE_SUFFIX_PATTERN.sub("", basename.lower())
    candidates: list[Path] = []
    for path in asset_dir.iterdir():
        if not path.is_file():
            continue
        active = _SIZE_SUFFIX_PATTERN.sub("", path.name.lower())
        if active == normalized or path.name.lower() == basename.lower():
            candidates.append(path)
    return sorted(candidates, key=lambda item: (item.stat().st_size <= 0, -item.stat().st_size))[0] if candidates else None


def _relative_markdown_path(path: Path, markdown_path: Path) -> str:
    return path.resolve().relative_to(markdown_path.parent.resolve()).as_posix()


def _download_remote_image(
    *,
    url: str,
    asset_dir: Path,
    markdown_path: Path,
    alt: str,
    fetcher: ImageFetcher,
    timeout: float,
) -> Path | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    if "ieeexplore.ieee.org" in url:
        headers["Referer"] = "https://ieeexplore.ieee.org/"
    try:
        payload, content_type = fetcher(url, headers, timeout)
    except Exception:
        return None
    if not payload or not _looks_like_image(payload, content_type):
        return None
    asset_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    source_stem = Path(unquote(parsed.path)).stem
    filename = _safe_stem(source_stem or alt or "image") + _guess_extension(url, content_type)
    target = asset_dir / filename
    if target.exists() and target.read_bytes() == payload:
        return target
    if target.exists():
        digest = hashlib.sha1(url.encode("utf-8", errors="replace")).hexdigest()[:8]
        target = target.with_name(f"{target.stem}-{digest}{target.suffix}")
    target.write_bytes(payload)
    return target


def validate_latex_structure(value: str) -> LatexValidation:
    text = str(value or "")
    depth = 0
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return LatexValidation(False, "unexpected_closing_brace")
    if depth != 0:
        return LatexValidation(False, "unclosed_brace")

    stack: list[str] = []
    for match in _ENV_TOKEN_PATTERN.finditer(text):
        name = match.group("name").strip()
        if match.group("kind") == "begin":
            stack.append(name)
            continue
        if not stack:
            return LatexValidation(False, f"unexpected_end:{name}")
        active = stack.pop()
        if active != name:
            return LatexValidation(False, f"environment_mismatch:{active}:{name}")
    if stack:
        return LatexValidation(False, f"unclosed_environment:{stack[-1]}")
    return LatexValidation(True, "")


def repair_latex_structure(value: str) -> tuple[str, bool, LatexValidation]:
    text = str(value or "").strip()
    initial = validate_latex_structure(text)
    if initial.valid:
        return text, False, initial

    # IEEE/Wiley HTML occasionally wraps a complete display environment in an
    # opening group but omits the matching final brace. Removing only that one
    # leading group is safe when the remainder is structurally valid.
    if re.match(r"^\{\s*\\begin\s*\{[^{}]+\}", text):
        candidate = text[1:].lstrip()
        validation = validate_latex_structure(candidate)
        if validation.valid:
            return candidate, True, validation

    return text, False, initial


def _repair_display_math(markdown: str, report: MarkdownRepairReport) -> str:
    def replace(match: re.Match[str]) -> str:
        body = match.group("body")
        repaired, changed, validation = repair_latex_structure(body)
        if changed:
            report.formulas_repaired += 1
            report.changed = True
            return "$$" + repaired + "$$"
        if not validation.valid:
            report.formulas_invalid += 1
        return match.group(0)

    return _DISPLAY_MATH_PATTERN.sub(replace, markdown)


def _protect_code_fences(markdown: str) -> tuple[str, list[str]]:
    fences: list[str] = []

    def replace(match: re.Match[str]) -> str:
        fences.append(match.group(0))
        return f"\n@@PAPER_FETCH_CODE_FENCE_{len(fences) - 1}@@\n"

    return _CODE_FENCE_PATTERN.sub(replace, markdown), fences


def _restore_code_fences(markdown: str, fences: list[str]) -> str:
    for index, block in enumerate(fences):
        markdown = markdown.replace(f"\n@@PAPER_FETCH_CODE_FENCE_{index}@@\n", block)
    return markdown


def repair_markdown_text(
    markdown: str,
    *,
    markdown_path: Path,
    fetcher: ImageFetcher | None = None,
    timeout: float = 45.0,
) -> tuple[str, MarkdownRepairReport]:
    path = Path(markdown_path)
    report = MarkdownRepairReport(path=str(path))
    protected, fences = _protect_code_fences(str(markdown or ""))
    protected = _repair_display_math(protected, report)

    matches = list(_IMAGE_PATTERN.finditer(protected))
    if not matches:
        return _restore_code_fences(protected, fences), report

    groups: dict[str, list[re.Match[str]]] = {}
    for match in matches:
        identity = _image_identity(match.group("alt"), match.group("dest"))
        groups.setdefault(identity, []).append(match)

    replacements: dict[tuple[int, int], str] = {}
    asset_dir = path.parent / f"{path.stem}_assets"
    active_fetcher = fetcher or _default_fetcher
    for group in groups.values():
        ordered = sorted(group, key=lambda item: _candidate_priority(item.group("dest")))
        chosen = ordered[0]
        chosen_replacement: str | None = None
        for candidate in ordered:
            destination = _strip_destination(candidate.group("dest"))
            remote = _absolute_remote_url(destination)
            local_path: Path | None = None
            if remote:
                local_path = _existing_asset_for_url(asset_dir, destination)
                if local_path is not None:
                    report.reused_local_images += 1
                else:
                    local_path = _download_remote_image(
                        url=remote,
                        asset_dir=asset_dir,
                        markdown_path=path,
                        alt=candidate.group("alt"),
                        fetcher=active_fetcher,
                        timeout=timeout,
                    )
                    if local_path is not None:
                        report.localized_images += 1
            else:
                local_candidate = (path.parent / destination).resolve(strict=False)
                if local_candidate.is_file():
                    local_path = local_candidate
            if local_path is None:
                continue
            relative = _relative_markdown_path(local_path, path)
            title = candidate.group("title")
            title_part = f' "{title}"' if title else ""
            chosen = candidate
            chosen_replacement = f"![{candidate.group('alt')}]({relative}{title_part})"
            break

        if chosen_replacement is None:
            destination = _absolute_remote_url(chosen.group("dest")) or _strip_destination(chosen.group("dest"))
            if _absolute_remote_url(chosen.group("dest")):
                label = chosen.group("alt") or "Image"
                chosen_replacement = f"[{label} source]({destination})"
                report.remote_images_unresolved += 1
                report.changed = True
            else:
                chosen_replacement = chosen.group(0)

        replacements[(chosen.start(), chosen.end())] = chosen_replacement
        for duplicate in group:
            if duplicate is chosen:
                continue
            replacements[(duplicate.start(), duplicate.end())] = ""
            report.duplicate_images_removed += 1
            report.changed = True
        if chosen_replacement != chosen.group(0):
            report.changed = True

    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(replacements):
        pieces.append(protected[cursor:start])
        pieces.append(replacements[(start, end)])
        cursor = end
    pieces.append(protected[cursor:])
    repaired = re.sub(r"\n{3,}", "\n\n", "".join(pieces))
    return _restore_code_fences(repaired, fences), report


def repair_markdown_file(
    path: str | Path,
    *,
    fetcher: ImageFetcher | None = None,
    timeout: float = 45.0,
) -> MarkdownRepairReport:
    markdown_path = Path(path)
    text = markdown_path.read_text(encoding="utf-8", errors="replace")
    repaired, report = repair_markdown_text(
        text,
        markdown_path=markdown_path,
        fetcher=fetcher,
        timeout=timeout,
    )
    if report.changed and repaired != text:
        temp = markdown_path.with_suffix(markdown_path.suffix + ".repair.tmp")
        temp.write_text(repaired, encoding="utf-8")
        temp.replace(markdown_path)
    return report


def install_markdown_save_hook() -> None:
    """Wrap the pipeline Markdown saver once so CLI and service outputs are repaired."""

    from .workflow import rendering

    current = rendering.save_markdown_to_disk
    if getattr(current, "_paper_fetch_markdown_repair_hook", False):
        return

    def wrapped(*args, **kwargs):
        path = current(*args, **kwargs)
        if path is not None:
            try:
                repair_markdown_file(path)
            except Exception:
                # Archival repair is best-effort; the original successfully written
                # Markdown remains available and acceptance reporting stays intact.
                pass
        return path

    setattr(wrapped, "_paper_fetch_markdown_repair_hook", True)
    rendering.save_markdown_to_disk = wrapped


__all__ = [
    "LatexValidation",
    "MarkdownRepairReport",
    "install_markdown_save_hook",
    "repair_latex_structure",
    "repair_markdown_file",
    "repair_markdown_text",
    "validate_latex_structure",
]
