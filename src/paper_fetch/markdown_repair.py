"""Repair archived Markdown images and safely recover malformed display math."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import mimetypes
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

_IMAGE_PATTERN = re.compile(
    r'!\[(?P<alt>[^\]]*)\]\((?P<dest><[^>]+>|[^)\s]+)'
    r'(?:\s+"(?P<title>[^"]*)")?\)',
    flags=re.IGNORECASE,
)
_DISPLAY_MATH_PATTERN = re.compile(r"\$\$(?P<body>.*?)\$\$", flags=re.DOTALL)
_ENV_PATTERN = re.compile(r"\\(?P<kind>begin|end)\s*\{(?P<name>[^{}]+)\}")
_SIZE_SUFFIX_PATTERN = re.compile(
    r"-(?:large|full|small|thumb|thumbnail|preview)(?=\.[A-Za-z0-9]+$)",
    flags=re.IGNORECASE,
)
_IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}

ImageFetcher = Callable[[str, dict[str, str], float], tuple[bytes, str]]


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


def _fenced_ranges(markdown: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    active: str | None = None
    offset = 0
    for line in markdown.splitlines(keepends=True):
        stripped = line.lstrip()
        fence = (
            "```"
            if stripped.startswith("```")
            else "~~~"
            if stripped.startswith("~~~")
            else None
        )
        if fence:
            if active is None:
                active = fence
                start = offset
            elif active == fence and start is not None:
                ranges.append((start, offset + len(line)))
                start = None
                active = None
        offset += len(line)
    if active is not None and start is not None:
        ranges.append((start, len(markdown)))
    return ranges


def _inside_ranges(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)


def _strip_destination(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        return text[1:-1].strip()
    return text


def _absolute_remote_url(value: str) -> str | None:
    destination = _strip_destination(value)
    if destination.startswith("//"):
        return "https:" + destination
    if destination.startswith(("http://", "https://")):
        return destination
    if destination.startswith("/mediastore/"):
        return "https://ieeexplore.ieee.org" + destination
    return None


def _normalized_asset_name(value: str) -> str:
    remote = _absolute_remote_url(value)
    parsed = urlparse(remote or _strip_destination(value))
    basename = unquote(Path(parsed.path).name).lower()
    return _SIZE_SUFFIX_PATTERN.sub("", basename)


def _image_identity(alt: str, destination: str) -> str:
    label = re.sub(r"\s+", " ", str(alt or "").strip().lower())
    basename = _normalized_asset_name(destination)
    if label:
        return f"{label}|{basename}"
    if basename:
        return basename
    return hashlib.sha1(destination.encode("utf-8", errors="replace")).hexdigest()


def _candidate_priority(destination: str) -> tuple[int, int]:
    text = _strip_destination(destination).lower()
    if re.search(r"-(?:large|full)(?=\.[a-z0-9]+(?:[?#]|$))", text):
        return (0, len(text))
    if re.search(
        r"-(?:small|thumb|thumbnail|preview)(?=\.[a-z0-9]+(?:[?#]|$))",
        text,
    ):
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
    if guessed == ".jpe":
        return ".jpg"
    return guessed if guessed in _IMAGE_EXTENSIONS else ".img"


def _looks_like_image(payload: bytes, content_type: str) -> bool:
    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return True
    prefixes = (
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"GIF87a",
        b"GIF89a",
        b"RIFF",
        b"BM",
    )
    return any(payload.startswith(prefix) for prefix in prefixes) or payload.lstrip().startswith(
        b"<svg"
    )


def _default_fetcher(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[bytes, str]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read()
        content_type = str(response.headers.get("Content-Type") or "")
    return body, content_type


def _existing_asset(asset_dir: Path, destination: str) -> Path | None:
    if not asset_dir.is_dir():
        return None
    parsed = urlparse(_absolute_remote_url(destination) or destination)
    basename = Path(unquote(parsed.path)).name
    normalized = _SIZE_SUFFIX_PATTERN.sub("", basename.lower())
    candidates = [
        path
        for path in asset_dir.iterdir()
        if path.is_file()
        and (
            path.name.lower() == basename.lower()
            or _SIZE_SUFFIX_PATTERN.sub("", path.name.lower()) == normalized
        )
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_size)


def _relative_markdown_path(path: Path, markdown_path: Path) -> str:
    return Path(os.path.relpath(path.resolve(), markdown_path.parent.resolve())).as_posix()


def _download_remote_image(
    *,
    url: str,
    asset_dir: Path,
    alt: str,
    fetcher: ImageFetcher,
    timeout: float,
) -> Path | None:
    headers = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126 Safari/537.36"
        ),
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
    source_stem = Path(unquote(urlparse(url).path)).stem
    filename = _safe_stem(source_stem or alt) + _guess_extension(url, content_type)
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
    if depth:
        return LatexValidation(False, "unclosed_brace")

    stack: list[str] = []
    for match in _ENV_PATTERN.finditer(text):
        name = match.group("name").strip()
        if match.group("kind") == "begin":
            stack.append(name)
        elif not stack:
            return LatexValidation(False, f"unexpected_end:{name}")
        else:
            active = stack.pop()
            if active != name:
                return LatexValidation(False, f"environment_mismatch:{active}:{name}")
    if stack:
        return LatexValidation(False, f"unclosed_environment:{stack[-1]}")
    return LatexValidation(True)


def repair_latex_structure(value: str) -> tuple[str, bool, LatexValidation]:
    text = str(value or "").strip()
    initial = validate_latex_structure(text)
    if initial.valid:
        return text, False, initial
    if re.match(r"^\{\s*\\begin\s*\{[^{}]+\}", text):
        candidate = text[1:].lstrip()
        validation = validate_latex_structure(candidate)
        if validation.valid:
            return candidate, True, validation
    return text, False, initial


def _repair_display_math(
    markdown: str,
    report: MarkdownRepairReport,
    fenced_ranges: list[tuple[int, int]],
) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _DISPLAY_MATH_PATTERN.finditer(markdown):
        if _inside_ranges(match.start(), fenced_ranges):
            continue
        body = match.group("body")
        repaired, changed, validation = repair_latex_structure(body)
        if not changed:
            if not validation.valid:
                report.formulas_invalid += 1
            continue
        pieces.extend((markdown[cursor : match.start()], "$$", repaired, "$$"))
        cursor = match.end()
        report.formulas_repaired += 1
        report.changed = True
    if not pieces:
        return markdown
    pieces.append(markdown[cursor:])
    return "".join(pieces)


def repair_markdown_text(
    markdown: str,
    *,
    markdown_path: Path,
    fetcher: ImageFetcher | None = None,
    timeout: float = 45.0,
) -> tuple[str, MarkdownRepairReport]:
    path = Path(markdown_path)
    report = MarkdownRepairReport(path=str(path))
    fenced_ranges = _fenced_ranges(markdown)
    repaired = _repair_display_math(markdown, report, fenced_ranges)
    fenced_ranges = _fenced_ranges(repaired)

    image_matches = [
        match
        for match in _IMAGE_PATTERN.finditer(repaired)
        if not _inside_ranges(match.start(), fenced_ranges)
    ]
    if not image_matches:
        return repaired, report

    groups: dict[str, list[re.Match[str]]] = {}
    for match in image_matches:
        identity = _image_identity(match.group("alt"), match.group("dest"))
        groups.setdefault(identity, []).append(match)

    replacements: dict[tuple[int, int], str] = {}
    asset_dir = path.parent / f"{path.stem}_assets"
    active_fetcher = fetcher or _default_fetcher
    for group in groups.values():
        ordered = sorted(group, key=lambda item: _candidate_priority(item.group("dest")))
        chosen = ordered[0]
        replacement: str | None = None
        for candidate in ordered:
            destination = _strip_destination(candidate.group("dest"))
            remote = _absolute_remote_url(destination)
            local_path: Path | None = None
            if remote:
                local_path = _existing_asset(asset_dir, destination)
                if local_path is not None:
                    report.reused_local_images += 1
                else:
                    local_path = _download_remote_image(
                        url=remote,
                        asset_dir=asset_dir,
                        alt=candidate.group("alt"),
                        fetcher=active_fetcher,
                        timeout=timeout,
                    )
                    if local_path is not None:
                        report.localized_images += 1
            else:
                candidate_path = (path.parent / destination).resolve(strict=False)
                if candidate_path.is_file():
                    local_path = candidate_path
            if local_path is None:
                continue
            relative = _relative_markdown_path(local_path, path)
            title = candidate.group("title")
            title_part = f' "{title}"' if title else ""
            chosen = candidate
            replacement = f"![{candidate.group('alt')}]({relative}{title_part})"
            break

        if replacement is None:
            source = _absolute_remote_url(chosen.group("dest"))
            if source:
                label = chosen.group("alt") or "Image"
                replacement = f"[{label} source]({source})"
                report.remote_images_unresolved += 1
                report.changed = True
            else:
                replacement = chosen.group(0)

        replacements[(chosen.start(), chosen.end())] = replacement
        if replacement != chosen.group(0):
            report.changed = True
        for duplicate in group:
            if duplicate is chosen:
                continue
            replacements[(duplicate.start(), duplicate.end())] = ""
            report.duplicate_images_removed += 1
            report.changed = True

    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(replacements):
        pieces.extend((repaired[cursor:start], replacements[(start, end)]))
        cursor = end
    pieces.append(repaired[cursor:])
    return re.sub(r"\n{3,}", "\n\n", "".join(pieces)), report


def repair_markdown_file(
    path: str | Path,
    *,
    fetcher: ImageFetcher | None = None,
    timeout: float = 45.0,
) -> MarkdownRepairReport:
    markdown_path = Path(path)
    original = markdown_path.read_text(encoding="utf-8", errors="replace")
    repaired, report = repair_markdown_text(
        original,
        markdown_path=markdown_path,
        fetcher=fetcher,
        timeout=timeout,
    )
    if report.changed and repaired != original:
        temporary = markdown_path.with_suffix(markdown_path.suffix + ".repair.tmp")
        temporary.write_text(repaired, encoding="utf-8")
        temporary.replace(markdown_path)
    return report


def install_markdown_save_hook() -> None:
    """Wrap the canonical saver once so CLI and embedded outputs are repaired."""

    from .workflow import rendering

    current = rendering.save_markdown_to_disk
    if getattr(current, "_paper_fetch_markdown_repair_hook", False):
        return

    def wrapped(*args, **kwargs):
        path = current(*args, **kwargs)
        if path is not None:
            try:
                repair_markdown_file(path)
            except Exception as exc:
                setattr(wrapped, "_paper_fetch_last_repair_error", str(exc))
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
