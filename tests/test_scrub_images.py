"""Tests for the scrub-images subcommand (v0.10.0)."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import pytest

from claude_recall import scrub_images


# --- Synthetic image fixtures -------------------------------------------------

def _png(width: int, height: int) -> bytes:
    """Build a minimal valid PNG of the requested dimensions.

    The image data is a single all-white scanline (we never decode it
    in scrub-images — only the IHDR is read). Crafted by hand rather
    than depending on Pillow.
    """
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload).to_bytes(4, "big")
        return len(payload).to_bytes(4, "big") + tag + payload + crc

    ihdr_payload = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"  # bit-depth=8, color=RGBA, no filter/interlace
    )
    raw = b"".join(
        b"\x00" + b"\xff" * (4 * width) for _ in range(height)
    )
    idat_payload = zlib.compress(raw)
    return (
        signature
        + chunk(b"IHDR", ihdr_payload)
        + chunk(b"IDAT", idat_payload)
        + chunk(b"IEND", b"")
    )


def _jpeg(width: int, height: int) -> bytes:
    """Minimal JPEG-like blob with a synthetic SOF0 marker.

    Real JPEGs are encoded; we only need the SOF dimensions to be
    parseable, so we emit SOI, SOF0 with the requested dimensions,
    and EOI. The scrub-images parser does not validate the rest.
    """
    soi = b"\xff\xd8"
    eoi = b"\xff\xd9"
    sof_len = 8 + 3  # 2 length + 1 precision + 2 height + 2 width + 3 component
    sof0_payload = (
        struct.pack(">H", sof_len)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x01\x11\x00"  # one component, sampling=0x11, qt=0
    )
    return soi + b"\xff\xc0" + sof0_payload + eoi


# --- Dimension parser tests ---------------------------------------------------


def test_png_dimensions_returns_width_height():
    data = _png(640, 480)
    assert scrub_images.png_dimensions(data) == (640, 480)


def test_png_dimensions_rejects_non_png():
    assert scrub_images.png_dimensions(b"not a png at all") is None


def test_png_dimensions_handles_truncated_input():
    assert scrub_images.png_dimensions(b"\x89PNG\r\n\x1a\n") is None


def test_jpeg_dimensions_returns_width_height():
    data = _jpeg(800, 600)
    assert scrub_images.jpeg_dimensions(data) == (800, 600)


def test_jpeg_dimensions_rejects_non_jpeg():
    assert scrub_images.jpeg_dimensions(b"\x89PNG\r\n\x1a\nfake") is None


def test_image_dimensions_dispatches_to_png():
    assert scrub_images.image_dimensions(_png(100, 200)) == (100, 200)


def test_image_dimensions_dispatches_to_jpeg():
    assert scrub_images.image_dimensions(_jpeg(300, 400)) == (300, 400)


def test_image_dimensions_returns_none_for_other_formats():
    assert scrub_images.image_dimensions(b"GIF87a" + b"\x00" * 20) is None


# --- Jsonl scrub tests --------------------------------------------------------

def _image_block(data: bytes, media_type: str = "image/png") -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "data": base64.b64encode(data).decode("ascii"),
            "media_type": media_type,
        },
    }


def _tool_result_turn(tool_use_id: str, image_block: dict, ts: str = "2026-06-11T19:00:00Z") -> dict:
    """Mirror the real jsonl shape: tool_result nested inside user message content."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": [image_block],
            }],
        },
        "timestamp": ts,
    }


def _write_session(path: Path, lines: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")


def test_scrub_session_finds_and_replaces_oversized_png(tmp_path):
    session_path = tmp_path / "test-session.jsonl"
    small = _tool_result_turn("toolu_01", _image_block(_png(640, 480)))
    big = _tool_result_turn("toolu_02", _image_block(_png(2048, 2048)))
    _write_session(session_path, [small, big])

    report = scrub_images.scrub_session(session_path, backup=False)

    assert report.sessions_modified == 1
    assert report.images_scanned == 2
    assert report.images_scrubbed == 1
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.width == 2048 and finding.height == 2048
    assert finding.reason == "dimension"

    # Verify the file: small image untouched, big one replaced with text.
    with open(session_path, encoding="utf-8") as f:
        rebuilt = [json.loads(line) for line in f]
    assert rebuilt[0]["message"]["content"][0]["content"][0]["type"] == "image"
    replaced = rebuilt[1]["message"]["content"][0]["content"][0]
    assert replaced["type"] == "text"
    assert scrub_images.PLACEHOLDER_PREFIX in replaced["text"]
    assert "2048x2048" in replaced["text"]


def test_scrub_session_is_idempotent(tmp_path):
    session_path = tmp_path / "idempotent.jsonl"
    big = _tool_result_turn("toolu_03", _image_block(_png(3000, 1500)))
    _write_session(session_path, [big])

    first = scrub_images.scrub_session(session_path, backup=False)
    assert first.images_scrubbed == 1

    second = scrub_images.scrub_session(session_path, backup=False)
    assert second.images_scrubbed == 0
    assert second.skipped_already_scrubbed >= 1


def test_scrub_session_preserves_tool_use_id(tmp_path):
    session_path = tmp_path / "linkage.jsonl"
    turn = _tool_result_turn("toolu_keep_this", _image_block(_png(4096, 2048)))
    _write_session(session_path, [turn])

    scrub_images.scrub_session(session_path, backup=False)

    with open(session_path, encoding="utf-8") as f:
        obj = json.loads(f.readline())
    result_block = obj["message"]["content"][0]
    assert result_block["tool_use_id"] == "toolu_keep_this"
    assert result_block["type"] == "tool_result"


def test_scrub_session_dry_run_does_not_modify_file(tmp_path):
    session_path = tmp_path / "dryrun.jsonl"
    big = _tool_result_turn("toolu_04", _image_block(_png(3000, 3000)))
    _write_session(session_path, [big])
    before = session_path.read_bytes()

    report = scrub_images.scrub_session(session_path, dry_run=True, backup=False)

    assert report.images_scrubbed == 1
    assert len(report.findings) == 1
    after = session_path.read_bytes()
    assert before == after


def test_scrub_session_writes_backup_by_default(tmp_path):
    session_path = tmp_path / "backup.jsonl"
    big = _tool_result_turn("toolu_05", _image_block(_png(2048, 2048)))
    _write_session(session_path, [big])

    scrub_images.scrub_session(session_path, backup=True)

    backups = list(tmp_path.glob("backup.jsonl.bak.*"))
    assert len(backups) == 1
    # The backup should contain the original image bytes.
    bak_text = backups[0].read_text(encoding="utf-8")
    assert '"type": "image"' in bak_text


def test_scrub_session_no_backup_when_no_changes(tmp_path):
    session_path = tmp_path / "no-change.jsonl"
    small = _tool_result_turn("toolu_06", _image_block(_png(800, 600)))
    _write_session(session_path, [small])

    scrub_images.scrub_session(session_path, backup=True)

    assert list(tmp_path.glob("no-change.jsonl.bak.*")) == []


def test_scrub_session_byte_size_threshold(tmp_path):
    """An image small in dimensions but large in bytes should be caught."""
    session_path = tmp_path / "bytes.jsonl"
    big_bytes_small_dim = _tool_result_turn(
        "toolu_07", _image_block(_png(500, 500)),
    )
    _write_session(session_path, [big_bytes_small_dim])

    # Set a max_bytes lower than the encoded PNG to trigger byte-size catch.
    report = scrub_images.scrub_session(
        session_path, max_bytes=100, backup=False,
    )

    assert report.images_scrubbed == 1
    assert report.findings[0].reason in {"byte_size", "both"}


def test_iter_session_files_filters_by_session_prefix(tmp_path):
    archive_root = tmp_path / "archive"
    proj = archive_root / "proj-slug"
    proj.mkdir(parents=True)
    (proj / "abc12345-deadbeef.jsonl").write_text("{}", encoding="utf-8")
    (proj / "def67890-cafebabe.jsonl").write_text("{}", encoding="utf-8")

    found = list(scrub_images.iter_session_files(
        archive_root, project_slug="proj-slug",
        session_id_prefix="abc12345",
    ))
    assert len(found) == 1
    assert found[0].stem.startswith("abc12345")


def test_run_scrub_walks_all_projects_when_slug_none(tmp_path):
    archive_root = tmp_path / "archive"
    for slug in ["proj-a", "proj-b"]:
        d = archive_root / slug
        d.mkdir(parents=True)
        path = d / f"{slug}-session.jsonl"
        _write_session(path, [
            _tool_result_turn(f"toolu_{slug}", _image_block(_png(2500, 2500))),
        ])

    report = scrub_images.run_scrub(archive_root, project_slug=None, backup=False)
    assert report.sessions_scanned == 2
    assert report.images_scrubbed == 2


def test_format_report_renders_findings(tmp_path):
    session_path = tmp_path / "fmt.jsonl"
    big = _tool_result_turn("toolu_fmt", _image_block(_png(2200, 2200)))
    _write_session(session_path, [big])

    report = scrub_images.scrub_session(session_path, dry_run=True, backup=False)
    output = scrub_images.format_report(report, dry_run=True)

    assert "DRY RUN" in output
    assert "2200x2200" in output
    assert "fmt.jsonl" in output
