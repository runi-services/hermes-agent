"""Materialize inline A2A files at receiver admission, never during text reads.

Uses the owning profile's native document cache (flat files for native pruning
and sandbox mounts), not a durable archive. URI references are never fetched.
"""

from __future__ import annotations

import base64
import binascii
import copy
import logging
import os
from pathlib import Path
import uuid

from gateway.platforms.base import get_document_cache_dir
from hermes_constants import get_hermes_dir

from . import protocol

# The HTTP receiver already caps the entire request at 1 MiB. Keep independent
# bounds here for direct callers and cap file count before creating any files.
MAX_BYTES = 1_048_576
MAX_FILES = 16


class FilePartError(ValueError):
    """Inline file could not be safely recovered; do not dispatch the task."""


def remove(paths: list[str]) -> None:
    """Remove only files created by this admission, best-effort on refusal."""
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).warning(
                "Could not remove an admitted A2A attachment"
            )


def materialize(
    params: dict, *, home: str | None = None
) -> tuple[str, list[str], list[str]]:
    """Return rendered text and aligned local attachment paths/media types.

    Validate all inline parts before writing; rollback this admission's files
    if any write fails. Original wire parts are left intact for task history.
    Paths use receiver-generated UUID/index-prefixed safe basenames,
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

    paths, types = [], []
    try:
        root = (
            get_hermes_dir("cache/documents", "document_cache", home=Path(home))
            if home is not None
            else get_document_cache_dir()
        )
        root.mkdir(parents=True, exist_ok=True)
        admission = uuid.uuid4().hex
        for index, data, basename, media_type in files:
            destination = root / f"a2a-file-{admission}-{index}-{basename}"
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            # Track immediately after exclusive creation, before write can fail.
            paths.append(str(destination))
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            types.append(media_type)
            # Render only a receiver-owned path, never raw bytes into the prompt.
            parts[index] = {
                "text": f"[file: {basename}] saved to {destination} ({len(data)} bytes)"
            }
        return protocol.extract_text(rendered), paths, types
    except OSError as exc:
        remove(paths)
        raise FilePartError("Inline file inbox write failed") from exc
