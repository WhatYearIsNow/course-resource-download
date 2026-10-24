"""Portable names, contained paths, and content-aware reuse."""

from __future__ import annotations

import hashlib
import os
import re

WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_NAME = 120


def safe_name(name, fallback="unnamed"):
    """Return one portable filename, including for malformed remote names."""
    value = str(name) if name is not None else ""
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f\ud800-\udfff]', "_", value).strip().rstrip(" .")
    if not value or value in {".", ".."}:
        value = (
            re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f\ud800-\udfff]', "_", str(fallback))
            .strip()
            .rstrip(" .")
            or "unnamed"
        )
        if value in {".", ".."}:
            value = "unnamed"
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        value = "_" + value
    stem, ext = os.path.splitext(value)
    if len(ext) > 20:
        stem, ext = value, ""
    # Limit both UTF-8 bytes (Unix) and characters (Windows).
    budget = MAX_NAME - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(" .")
    return (stem or "unnamed") + ext


def stable_id(url):
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:10]


def safe_join(directory, filename):
    """Join an untrusted manifest basename; refuse traversal and symlink escape."""
    if not isinstance(filename, str) or not filename:
        return None
    try:
        if len(filename.encode("utf-8")) > 255:
            return None
    except UnicodeError:
        return None
    if filename in {".", ".."} or filename != filename.rstrip(" ."):
        return None
    if re.search(r'[\\/:*?"<>|\x00-\x1f\x7f]', filename):
        return None
    if filename.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        return None
    try:
        root = os.path.realpath(directory)
        path = os.path.join(os.fspath(directory), filename)
        resolved = os.path.realpath(path)
        if os.path.commonpath([root, resolved]) != root or resolved == root:
            return None
        return path
    except (OSError, ValueError, TypeError):
        return None


def unique_path(directory, filename, source_id=None):
    """Allocate a vacant basename. Caller must hold the directory lock until write."""
    filename = safe_name(filename)
    path = safe_join(directory, filename)
    if path and not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(filename)
    tag = safe_name(source_id or "dup")[:20]
    for number in range(1, 10000):
        suffix = f"__{tag}" + (f"_{number}" if number > 1 else "")
        budget = MAX_NAME - len((suffix + ext).encode("utf-8"))
        prefix = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        candidate = safe_name(prefix + suffix + ext)
        path = safe_join(directory, candidate)
        if path and not os.path.lexists(path):
            return path
    raise RuntimeError("Cannot allocate a vacant output filename")


def is_usable(path):
    from .verify import check_file

    return check_file(path)[0]


def claim_or_allocate(directory, filename, url, index):
    previous = index.get(url) if isinstance(index, dict) and isinstance(url, str) else None
    path = safe_join(directory, previous)
    if path and not os.path.islink(path):
        if is_usable(path):
            return path, True
        # A failed interrupted prior download is still owned by this URL. Repair
        # it in place, preserving bytes until a replacement passes validation.
        if not os.path.lexists(path) or os.path.isfile(path):
            return path, False
    return unique_path(directory, filename, source_id=stable_id(url)), False
