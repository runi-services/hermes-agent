"""Materialize inline A2A files at receiver admission, never during text reads.

Uses the owning profile's scratch directory, not a durable attachment archive.
URI references remain references: this module performs no network fetches.
"""

from __future__ import annotations

import base64
import binascii
import copy
import os
from pathlib import Path
import shutil
import tempfile

from hermes_constants import get_hermes_home

from . import protocol

# The HTTP receiver already caps the entire request at 1 MiB. Keep independent
# bounds here for direct callers and cap file count before creating any files.
MAX_BYTES = 1_048_576
MAX_FILES = 16


class FilePartError(ValueError):
    """Inline file could not be safely recovered; do not dispatch the task."""


def materialize(
    params: dict, *, home: str | None = None
) -> tuple[str, list[str], list[str]]:
    """Return rendered text and aligned local attachment paths/media types.

    Validate all inline parts before writing; rollback this admission's directory
    if any write fails. Original wire parts are left intact for task history.
    Paths use a receiver-generated unique directory and index-prefixed basenames,
    not peer-supplied task IDs or filesystem paths.
    """
    rendered = copy.deepcopy(params)
    message = rendered.get("message", rendered)
    parts = message.get("parts", []) if isinstance(message, dict) else []
    files = []
    total = 0
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        legacy = part.get("file")
        if "raw" in part:
            raw = part["raw"]
            filename = part.get("filename") or part.get("name")
            media_type = part.get("mediaType") or part.get("mimeType")
        elif isinstance(legacy, dict) and "bytes" in legacy:
            raw = legacy["bytes"]
            filename = legacy.get("name")
            media_type = legacy.get("mimeType")
        else:
            continue
        if len(files) >= MAX_FILES:
            raise FilePartError("Too many inline files")
        if not isinstance(raw, str) or len(raw) > 4 * ((MAX_BYTES + 2) // 3):
            raise FilePartError("Invalid or oversized inline file")
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise FilePartError("Invalid inline file base64") from exc
        total += len(data)
        if total > MAX_BYTES:
            raise FilePartError("Inline files exceed byte limit")
        # Neither slash convention nor dot components may escape the directory.
        basename = (
            str(filename or "attachment.bin").replace("\\", "/").rsplit("/", 1)[-1]
        )
        basename = "".join(
            c for c in basename if c.isascii() and (c.isalnum() or c in "._-")
        )[:120]
        basename = basename.strip(".") or "attachment.bin"
        media_type = (
            media_type if isinstance(media_type, str) else "application/octet-stream"
        )
        files.append((index, data, basename, media_type or "application/octet-stream"))

    if not files:
        return protocol.extract_text(params), [], []

    directory = None
    paths, types = [], []
    try:
        root = Path(home or get_hermes_home()) / "cache" / "scratch"
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="a2a-file-", dir=root))
        for index, data, basename, media_type in files:
            destination = directory / f"{index}-{basename}"
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            paths.append(str(destination))
            types.append(media_type)
            # Render only a receiver-owned path, never raw bytes into the prompt.
            parts[index] = {
                "text": f"[file: {basename}] saved to {destination} ({len(data)} bytes)"
            }
        return protocol.extract_text(rendered), paths, types
    except OSError as exc:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise FilePartError("Inline file inbox write failed") from exc
