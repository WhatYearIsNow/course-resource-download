"""Atomic manifests with independent discovery and persistent file ownership."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from .files import safe_join

SCHEMA_VERSION = 1
STATUSES = frozenset(
    {
        "ok",
        "exists",
        "discovered",
        "failed",
        "incomplete",
        "permission_denied",
        "skipped",
        "skipped_video",
        "skipped_ebook",
        "skipped_non_document",
        "skipped_unsupported_format",
        "unsupported",
        "unknown",
    }
)
FAILURE_STATUSES = frozenset(
    {"failed", "incomplete", "permission_denied", "unsupported", "unknown"}
)


def _read_manifest(directory):
    try:
        with open(os.path.join(directory, "_manifest.json"), encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, UnicodeError):
        return {}


def _status(entry):
    status = entry.get("status") if isinstance(entry, dict) else None
    return status if isinstance(status, str) and status in STATUSES else "unknown"


def summarize(entries):
    """Return (counts, has_failures); discovered/skipped are deliberate outcomes."""
    counts = {}
    for entry in entries:
        status = _status(entry)
        counts[status] = counts.get(status, 0) + 1
    return counts, any(counts.get(status, 0) for status in FAILURE_STATUSES)


def _ownership(directory, data):
    candidates = []
    if isinstance(data, dict):
        ownership = data.get("ownership")
        if isinstance(ownership, dict):
            candidates.extend(ownership.items())
        resources = data.get("resources", [])
    else:
        resources = data
    if isinstance(resources, list):
        for entry in resources:
            if isinstance(entry, dict) and _status(entry) in {"ok", "exists"}:
                candidates.append((entry.get("url"), entry.get("file")))
    index = {}
    for url, filename in candidates:
        if isinstance(url, str) and url and safe_join(directory, filename):
            index[url] = filename
    # Ambiguous ownership is unsafe: never reuse another source's output.
    owners = {}
    for url, filename in index.items():
        owners.setdefault(filename.casefold(), []).append(url)
    return {
        url: filename for url, filename in index.items() if len(owners[filename.casefold()]) == 1
    }


def load_manifest_index(directory):
    """Read v1 or legacy manifests, tolerating malformed records and unsafe paths."""
    return _ownership(directory, _read_manifest(directory))


def _normalize(entries, directory):
    if not isinstance(entries, (list, tuple)):
        raise TypeError("entries must be a list or tuple of resource records")
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            result.append({"status": "unknown", "reason": "Malformed resource record"})
            continue
        row = dict(entry)
        row["status"] = _status(row)
        if row.get("file") is not None and not safe_join(directory, row["file"]):
            row["status"] = "failed"
            row["file"] = None
            row["reason"] = "Unsafe output filename"
        result.append(row)
    return result


def write_manifest(directory, entries, tasks=None, *, list_only=False, folders=None):
    """Atomically publish one run report; preserve known ownership across runs.

    The caller holds the directory lock for the full traversal/download/write.
    Enumeration writes only _discovery.json and never changes _manifest.json.
    """
    os.makedirs(directory, exist_ok=True)
    resources = _normalize(entries, directory)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "discovery" if list_only else "download",
        "resources": resources,
        "statistics": summarize(resources)[0],
    }
    if not list_only:
        prior = load_manifest_index(directory)
        for row in resources:
            url, filename = row.get("url"), row.get("file")
            if (
                isinstance(url, str)
                and url
                and row["status"] in {"ok", "exists"}
                and safe_join(directory, filename)
            ):
                prior[url] = filename
        payload["ownership"] = _ownership(directory, {"ownership": prior})
    if tasks is not None:
        payload["tasks"] = tasks
    if folders is not None:
        payload["folders"] = folders
    name = "_discovery.json" if list_only else "_manifest.json"
    path = os.path.join(directory, name)
    fd, temporary = tempfile.mkstemp(prefix=".crd-manifest-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path
