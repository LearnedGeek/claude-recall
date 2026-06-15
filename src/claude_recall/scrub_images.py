"""Scrub oversized images out of session jsonls (v0.10.0).

The Anthropic API enforces a 2000px-on-any-dimension limit when a
conversation has multiple images ("many-image requests"). Once an
oversized image is in the session transcript, every subsequent request
fails with::

  An image in the conversation exceeds the dimension limit for
  many-image requests (2000px). Start a new session with fewer images.

The user-visible fix the error suggests — start a new session — drops
the conversation. `claude-recall scrub-images` is the surgical fix:
walk the project's session jsonls, find image content blocks exceeding
the limit, replace each one with a text placeholder that keeps the
`tool_use_id` linkage intact. The session resumes cleanly with the
oversized images gone and the rest of the history preserved.

Detection:

- PNG dimensions read from the IHDR chunk (bytes 16-24).
- JPEG dimensions read from the first SOFn marker (excluding DHT/DAC/DNL).
- Base64-decode happens on demand; non-image content is skipped.
- Recursive walk catches images nested inside ``tool_result`` content
  blocks (the common case — screenshots returned from Read tool calls).

Scrubbing:

- Each oversized image is replaced with a single text block reading
  ``[image removed by claude-recall scrub-images: <WxH> exceeded
  <limit>px many-image limit]``.
- The enclosing ``tool_result`` / ``message.content`` structure is
  preserved so resume works without orphan ``tool_use`` blocks.
- ``toolUseResult.file.base64`` (Claude Code-side metadata, not part
  of the API conversation) is also blanked to reduce file size.
- Writes are atomic: temp file then ``os.replace``.
- A ``.bak.<timestamp>`` sidecar is written before any modification
  unless ``--no-backup`` is passed.

Idempotency:

- The text placeholder is detected on re-runs and skipped, so running
  ``scrub-images`` repeatedly is safe and a no-op after the first
  successful pass.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .projects import slug_from_path

PLACEHOLDER_PREFIX = "[image removed by claude-recall scrub-images:"

# Anthropic's many-image dimension limit (Jun 2026).
DEFAULT_MAX_DIM_PX = 2000

# Soft byte cap: catches base64 payloads that decode under 2000px but
# are unusually large (e.g., near-uncompressed PNGs at 1990x1990). Set
# generously; can be tightened per-invocation.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB


@dataclass
class ImageFinding:
    """One oversized image found in a session jsonl."""

    session_path: Path
    line_number: int
    width: int | None
    height: int | None
    byte_size: int
    reason: str             # "dimension" | "byte_size" | "both"
    timestamp: str = ""     # the turn's timestamp from the jsonl, if present


@dataclass
class ScrubReport:
    """Outcome of one scrub-images run."""

    sessions_scanned: int = 0
    sessions_modified: int = 0
    images_scanned: int = 0
    images_scrubbed: int = 0
    findings: list[ImageFinding] = field(default_factory=list)
    skipped_already_scrubbed: int = 0
    parse_errors: list[tuple[Path, int, str]] = field(default_factory=list)

    @property
    def had_changes(self) -> bool:
        return self.images_scrubbed > 0


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) of a PNG, or None if not a valid PNG.

    PNG signature is 8 bytes followed by the IHDR chunk whose payload
    starts at byte 16 with two big-endian uint32s for width and height.
    """
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        width, height = struct.unpack(">II", data[16:24])
    except struct.error:
        return None
    return width, height


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) of a JPEG, or None if not a valid JPEG.

    Scans the segment chain for the first SOFn marker that carries
    dimensions. SOF0/1/2/3/5/6/7/9/10/11/13/14/15 all encode dimensions
    identically; we exclude DHT (0xC4), DAC (0xCC), and DNL (0xC8)
    which share the SOFn range but are not start-of-frame.
    """
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        # Skip fill bytes (0xFF followed by 0xFF).
        while i < n - 1 and data[i + 1] == 0xFF:
            i += 1
        if i >= n - 1:
            return None
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            # SOFn: 2 marker bytes + 2 length + 1 precision + 2 height + 2 width
            if i + 9 > n:
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        # Non-frame marker — skip its segment.
        if i + 4 > n:
            return None
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + seg_len
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) for PNG or JPEG bytes, else None."""
    dims = png_dimensions(data)
    if dims is not None:
        return dims
    return jpeg_dimensions(data)


def _iter_image_blocks(node) -> Iterator[dict]:
    """Yield every dict in *node* with ``type == 'image'`` and a base64 source.

    Walks lists and dicts recursively so images nested inside
    ``tool_result.content[...]`` are found.
    """
    if isinstance(node, dict):
        if (
            node.get("type") == "image"
            and isinstance(node.get("source"), dict)
            and node["source"].get("type") == "base64"
        ):
            yield node
        for v in node.values():
            yield from _iter_image_blocks(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_image_blocks(v)


def _is_placeholder_text_block(block: dict) -> bool:
    """True if *block* is a text block emitted by a prior scrub run."""
    if block.get("type") != "text":
        return False
    text = block.get("text", "")
    return isinstance(text, str) and text.startswith(PLACEHOLDER_PREFIX)


def _make_placeholder(width: int | None, height: int | None, byte_size: int,
                      reason: str, max_dim_px: int, max_bytes: int) -> dict:
    """Build the replacement text block for an oversized image."""
    if width and height:
        dims = f"{width}x{height}"
    else:
        dims = "unknown dimensions"
    text = (
        f"{PLACEHOLDER_PREFIX} {dims} ({byte_size:,} bytes) "
        f"exceeded limits (dim>{max_dim_px}px or bytes>{max_bytes:,}, "
        f"reason={reason})]"
    )
    return {"type": "text", "text": text}


def _scrub_image_block(block: dict, finding: ImageFinding, max_dim_px: int,
                       max_bytes: int) -> None:
    """Replace *block*'s image source/data with a placeholder text.

    Mutates the block in place: changes ``type`` to ``text``, removes
    ``source``, adds ``text``.
    """
    placeholder = _make_placeholder(
        finding.width, finding.height, finding.byte_size,
        finding.reason, max_dim_px, max_bytes,
    )
    block.clear()
    block.update(placeholder)


def _scan_and_patch_line(line: str, session_path: Path, line_number: int,
                        max_dim_px: int, max_bytes: int,
                        report: ScrubReport) -> tuple[str, bool]:
    """Process one jsonl line. Returns (new_line, modified)."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        report.parse_errors.append((session_path, line_number, str(exc)))
        return line, False

    modified = False
    timestamp = obj.get("timestamp", "") if isinstance(obj, dict) else ""

    # Check existing placeholders for idempotency. Walk the same tree
    # the image walker would.
    if isinstance(obj, dict):
        for placeholder in _iter_placeholder_blocks(obj):
            report.skipped_already_scrubbed += 1

    for block in list(_iter_image_blocks(obj)):
        report.images_scanned += 1
        src = block.get("source", {})
        b64 = src.get("data", "")
        if not isinstance(b64, str) or not b64:
            continue
        # Cheap byte-size estimate before decode: base64 is 4/3 the
        # binary size, so binary ≈ len(b64) * 3 / 4.
        approx_bytes = (len(b64) * 3) // 4
        try:
            raw = base64.b64decode(b64, validate=False)
        except (binascii.Error, ValueError):
            continue
        byte_size = len(raw)
        dims = image_dimensions(raw)
        width, height = (dims if dims is not None else (None, None))

        over_dim = bool(dims and (width > max_dim_px or height > max_dim_px))
        over_bytes = byte_size > max_bytes
        if not (over_dim or over_bytes):
            continue

        reason = (
            "both" if over_dim and over_bytes
            else ("dimension" if over_dim else "byte_size")
        )
        finding = ImageFinding(
            session_path=session_path,
            line_number=line_number,
            width=width,
            height=height,
            byte_size=byte_size,
            reason=reason,
            timestamp=timestamp,
        )
        report.findings.append(finding)
        _scrub_image_block(block, finding, max_dim_px, max_bytes)
        report.images_scrubbed += 1
        modified = True

    # Also blank toolUseResult.file.base64 if it's huge — Claude Code
    # metadata, doesn't affect the API conversation but bloats the file.
    if isinstance(obj, dict):
        tur = obj.get("toolUseResult")
        if isinstance(tur, dict):
            f = tur.get("file")
            if isinstance(f, dict) and isinstance(f.get("base64"), str):
                if len(f["base64"]) > 1024 and modified:
                    f["base64"] = "[removed by claude-recall scrub-images]"

    if not modified:
        return line, False
    return json.dumps(obj, ensure_ascii=False) + "\n", True


def _iter_placeholder_blocks(node) -> Iterator[dict]:
    """Yield blocks that look like prior-run placeholders."""
    if isinstance(node, dict):
        if _is_placeholder_text_block(node):
            yield node
        for v in node.values():
            yield from _iter_placeholder_blocks(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_placeholder_blocks(v)


def scrub_session(session_path: Path, *, max_dim_px: int = DEFAULT_MAX_DIM_PX,
                  max_bytes: int = DEFAULT_MAX_BYTES, dry_run: bool = False,
                  backup: bool = True, report: ScrubReport | None = None,
                  ) -> ScrubReport:
    """Scrub oversized images out of one session jsonl.

    If *dry_run* is True, no file changes are made; the report still
    lists every finding the scrub *would* make.
    """
    if report is None:
        report = ScrubReport()
    report.sessions_scanned += 1

    if not session_path.is_file():
        return report

    tmp_path = session_path.with_suffix(session_path.suffix + ".scrub.tmp")
    bak_path: Path | None = None
    line_modified_count = 0

    with open(session_path, "r", encoding="utf-8") as r, \
         open(tmp_path, "w", encoding="utf-8", newline="") as w:
        for lineno, raw_line in enumerate(r, start=1):
            new_line, modified = _scan_and_patch_line(
                raw_line, session_path, lineno, max_dim_px, max_bytes, report,
            )
            if modified:
                line_modified_count += 1
            w.write(new_line)

    if line_modified_count == 0 or dry_run:
        # Nothing to write; clean up the temp file.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return report

    if backup:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        bak_path = session_path.with_suffix(session_path.suffix + f".bak.{ts}")
        shutil.copy2(session_path, bak_path)

    os.replace(tmp_path, session_path)
    report.sessions_modified += 1
    return report


def iter_session_files(archive_root: Path, project_slug: str | None = None,
                       session_id_prefix: str | None = None) -> Iterator[Path]:
    """Yield session jsonl paths in scope.

    - With *project_slug*: walk only that project's archive directory.
    - Without: walk every project directory under *archive_root*.
    - With *session_id_prefix*: filter to files whose stem starts with
      the prefix. Useful for the ``--session abc12345`` form.
    """
    if not archive_root.is_dir():
        return
    if project_slug:
        roots = [archive_root / project_slug]
    else:
        roots = [p for p in archive_root.iterdir() if p.is_dir()]
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            if session_id_prefix and not path.stem.startswith(session_id_prefix):
                continue
            yield path


def run_scrub(archive_root: Path, *, project_slug: str | None = None,
              session_id_prefix: str | None = None,
              max_dim_px: int = DEFAULT_MAX_DIM_PX,
              max_bytes: int = DEFAULT_MAX_BYTES,
              dry_run: bool = False, backup: bool = True,
              ) -> ScrubReport:
    """Top-level entry: scrub every session jsonl in scope."""
    report = ScrubReport()
    for session_path in iter_session_files(
        archive_root, project_slug=project_slug,
        session_id_prefix=session_id_prefix,
    ):
        scrub_session(
            session_path,
            max_dim_px=max_dim_px,
            max_bytes=max_bytes,
            dry_run=dry_run,
            backup=backup,
            report=report,
        )
    return report


def format_report(report: ScrubReport, dry_run: bool = False) -> str:
    """Human-readable summary."""
    lines: list[str] = []
    if dry_run:
        lines.append("[DRY RUN] no files modified")
    lines.append(
        f"sessions: {report.sessions_scanned} scanned, "
        f"{report.sessions_modified} modified"
    )
    lines.append(
        f"images:   {report.images_scanned} scanned, "
        f"{report.images_scrubbed} scrubbed, "
        f"{report.skipped_already_scrubbed} already-scrubbed"
    )
    if report.findings:
        lines.append("")
        lines.append("findings:")
        for f in report.findings:
            dims = f"{f.width}x{f.height}" if f.width and f.height else "?x?"
            lines.append(
                f"  {f.session_path.name}:{f.line_number} "
                f"{dims} ({f.byte_size:,} bytes, reason={f.reason})"
            )
    if report.parse_errors:
        lines.append("")
        lines.append("parse errors (skipped lines):")
        for path, lineno, exc in report.parse_errors[:5]:
            lines.append(f"  {path.name}:{lineno} {exc}")
        if len(report.parse_errors) > 5:
            lines.append(f"  ... and {len(report.parse_errors) - 5} more")
    return "\n".join(lines)
