"""Assemble an HLS stream into one MP4 without re-encoding.

The platform exposes video only as an HLS playlist. Segments are appended in
playlist order into one MPEG-TS staging file, then ffmpeg copies the elementary
streams into an MP4 container; nothing is decoded, so quality is unchanged.

ffmpeg is an external binary and is located explicitly. A missing binary is a
reported failure, never a silent fallback to an unverified file: the assembled
result is only published after the same structural check a direct download gets.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from urllib.parse import urljoin

from .download import fetch, stream_to, validate_url
from .verify import check_file

FFMPEG_ENV = "CRD_FFMPEG"
DEFAULT_HLS_LIMIT = 4 * 1024 * 1024 * 1024
FFMPEG_TIMEOUT = 1800


def find_ffmpeg():
    """Return the ffmpeg path from ``CRD_FFMPEG`` or ``PATH``; raise when absent."""
    configured = os.environ.get(FFMPEG_ENV)
    if configured:
        if not os.path.isfile(configured):
            raise RuntimeError(f"{FFMPEG_ENV} does not point to an existing file")
        return configured
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError(
            "ffmpeg is required to convert an HLS stream into MP4; install ffmpeg or set "
            + FFMPEG_ENV
        )
    return found


def _attributes(line):
    """Parse ``KEY=VALUE`` attributes from one playlist tag, ignoring quoting."""
    body = line.split(":", 1)[1] if ":" in line else ""
    attributes = {}
    for item in body.split(","):
        key, separator, value = item.partition("=")
        if separator:
            attributes[key.strip().upper()] = value.strip().strip('"')
    return attributes


def parse_master_playlist(text, base_url):
    """Return the highest-bandwidth variant URL from a master playlist, or None."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        try:
            bandwidth = int(_attributes(line).get("BANDWIDTH", "0"))
        except ValueError:
            bandwidth = 0
        for following in lines[index + 1 :]:
            if not following.startswith("#"):
                variants.append((bandwidth, following))
                break
    if not variants:
        return None
    variants.sort(key=lambda item: item[0])
    return urljoin(base_url, variants[-1][1])


def parse_media_playlist(text, base_url):
    """Return ``(segment URLs, init-map URL or None)`` in playlist order."""
    segments, init = [], None
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if line.startswith("#EXT-X-MAP"):
            uri = _attributes(line).get("URI")
            if uri:
                init = urljoin(base_url, uri)
        elif not line.startswith("#"):
            segments.append(urljoin(base_url, line))
    return segments, init


def resolve_playlist(url, referer=None, timeout=180):
    """Follow a master playlist if needed; return ``(segments, init)``."""
    text = fetch(url, referer=referer, timeout=timeout).decode("utf-8", "replace")
    base = url
    if "#EXT-X-STREAM-INF" in text:
        variant = parse_master_playlist(text, url)
        if not variant:
            raise ValueError("Master playlist has no variant stream")
        validate_url(variant)
        base = variant
        text = fetch(variant, referer=referer, timeout=timeout).decode("utf-8", "replace")
    segments, init = parse_media_playlist(text, base)
    for item in ([init] if init else []) + segments:
        validate_url(item)
    return segments, init


def _remux(binary, source, dest):
    """Copy the staging transport stream into MP4; never re-encode."""
    base = [binary, "-y", "-loglevel", "error", "-f", "mpegts", "-i", source, "-c", "copy"]
    attempts = (
        base + ["-bsf:a", "aac_adtstoasc", "-f", "mp4", dest],
        base + ["-f", "mp4", dest],
    )
    detail = ""
    for command in attempts:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg timed out while converting the stream") from None
        except OSError as exc:
            raise RuntimeError("Cannot run ffmpeg: " + type(exc).__name__) from None
        if result.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest):
            return
        detail = (result.stderr or "").strip()[-300:]
    raise RuntimeError("ffmpeg could not convert the stream into MP4: " + (detail or "no detail"))


def download_hls(
    url,
    dest,
    *,
    referer=None,
    timeout=180,
    max_bytes=None,
    ffmpeg=None,
    chunk=1 << 20,
):
    """Download an HLS stream into one MP4; verify before replacing ``dest``.

    ``max_bytes`` bounds the assembled stream, not one segment. Failure removes
    the staging files and leaves any existing destination untouched.
    """
    limit = DEFAULT_HLS_LIMIT if max_bytes is None else max_bytes
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("max_bytes must be a positive integer")
    binary = ffmpeg or find_ffmpeg()
    segments, init = resolve_playlist(url, referer=referer, timeout=timeout)
    if not segments:
        raise ValueError("HLS playlist has no media segments")
    directory = os.path.dirname(os.fspath(dest)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=".crd-download-", suffix=".ts.part", dir=directory)
    output = staging[: -len(".ts.part")] + ".mp4.part"
    try:
        total = 0
        with os.fdopen(fd, "wb") as stream:
            for part in ([init] if init else []) + segments:
                remaining = limit - total
                if remaining <= 0:
                    raise ValueError(f"Assembled stream exceeds byte limit ({limit})")
                total += stream_to(
                    part,
                    stream.write,
                    referer=referer,
                    timeout=timeout,
                    max_bytes=remaining,
                    chunk=chunk,
                )
            stream.flush()
            os.fsync(stream.fileno())
        _remux(binary, staging, output)
        ok, reason, _ = check_file(output, extension=".mp4")
        if not ok:
            raise ValueError("Assembled video failed validation: " + reason)
        os.replace(output, dest)
        return total
    finally:
        for path in (staging, output):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
