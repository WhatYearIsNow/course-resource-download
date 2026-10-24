"""Stable adapter facade. Browser packages are deliberately absent from core."""

from .download import download_atomic, fetch, safe_error
from .files import claim_or_allocate, is_usable, safe_join, safe_name, stable_id, unique_path
from .locking import acquire_lock, cleanup_stale_lock_files, cleanup_stale_parts, release_lock
from .manifest import load_manifest_index, summarize, write_manifest

__all__ = [
    "safe_name",
    "stable_id",
    "unique_path",
    "is_usable",
    "safe_join",
    "cleanup_stale_parts",
    "acquire_lock",
    "release_lock",
    "cleanup_stale_lock_files",
    "load_manifest_index",
    "claim_or_allocate",
    "write_manifest",
    "summarize",
    "download_atomic",
    "fetch",
    "safe_error",
]
