"""Bounded HTTP(S) transfers, with validation before atomic publication."""

from __future__ import annotations

import http.client
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_FETCH_LIMIT = 32 * 1024 * 1024
DEFAULT_DOWNLOAD_LIMIT = 512 * 1024 * 1024


def validate_url(url):
    """Accept HTTP(S) only; credentials/control characters are never valid inputs."""
    if not isinstance(url, str) or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("A valid HTTP(S) URL is required")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("A valid HTTP(S) URL without embedded credentials is required") from None
    return url


def redact_url(url):
    """Remove credentials, fragments, and signed query values from a URL."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or "redacted"
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        return urllib.parse.urlunsplit(
            (parsed.scheme, host, parsed.path, "[redacted]" if parsed.query else "", "")
        )
    except ValueError:
        return "[redacted URL]"


def safe_error(exc):
    """Safe, bounded user-facing exception text; signed URLs never enter logs."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "Network request failed"
    message = str(exc)
    message = re.sub(
        r"(?:https?|file|ftp)://[^\s<>\"']+",
        lambda match: redact_url(match.group(0)),
        message,
        flags=re.I,
    )
    # Some libraries report only a query fragment or dictionary of parameters.
    message = re.sub(
        r"(?i)((?:token|signature|authorization|password|secret|credential|key)[\w-]*\s*[=:]\s*)[^\s,;}]+",
        r"\1[redacted]",
        message,
    )
    return message[:300]


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        if (
            urllib.parse.urlsplit(req.full_url).scheme == "https"
            and urllib.parse.urlsplit(newurl).scheme == "http"
        ):
            raise ValueError("Refusing HTTPS to HTTP redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _parameters(timeout, max_bytes, default_limit, chunk):
    limit = default_limit if max_bytes is None else max_bytes
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if (
        isinstance(chunk, bool)
        or not isinstance(chunk, int)
        or chunk <= 0
        or chunk > 8 * 1024 * 1024
    ):
        raise ValueError("chunk must be between 1 byte and 8 MiB")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 0 < timeout < float("inf")
    ):
        raise ValueError("timeout must be a finite positive number")
    return limit


def _open(url, referer, timeout):
    validate_url(url)
    headers = {"User-Agent": "course-resource-download", "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = validate_url(referer)
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.build_opener(_SafeRedirect()).open(request, timeout=timeout)


def _transfer(response, write, limit, chunk):
    validate_url(response.geturl())
    status = response.getcode()
    if status != 200:
        raise ValueError(f"Expected HTTP 200, received {status}")
    encoding = response.headers.get("Content-Encoding", "identity").lower().strip()
    if encoding not in {"identity", ""}:
        raise ValueError("Unexpected HTTP content encoding")
    lengths = response.headers.get_all("Content-Length", [])
    if len(lengths) > 1:
        raise ValueError("Ambiguous HTTP Content-Length headers")
    if lengths and response.headers.get("Transfer-Encoding"):
        raise ValueError("Ambiguous HTTP transfer framing")
    raw_length = lengths[0] if lengths else None
    length = None
    if raw_length is not None:
        if not re.fullmatch(r"[0-9]+", raw_length.strip()):
            raise ValueError("Invalid HTTP Content-Length")
        length = int(raw_length)
        if length > limit:
            raise ValueError(f"Resource exceeds byte limit ({limit})")
    total = 0
    while True:
        try:
            data = response.read(min(chunk, limit - total + 1))
        except http.client.IncompleteRead:
            raise ValueError("Truncated HTTP response") from None
        if not data:
            break
        total += len(data)
        if total > limit:
            raise ValueError(f"Resource exceeds byte limit ({limit})")
        write(data)
    if length is not None and length != total:
        raise ValueError(f"Truncated HTTP response: expected {length} bytes, received {total}")
    if not total:
        raise ValueError("Empty HTTP response")
    return total


def fetch(url, referer=None, timeout=180, max_bytes=None):
    """Fetch at most 32 MiB by default. Limits fail; data is never silently cut."""
    limit = _parameters(timeout, max_bytes, DEFAULT_FETCH_LIMIT, 1 << 20)
    chunks = []
    with _open(url, referer, timeout) as response:
        _transfer(response, chunks.append, limit, 1 << 20)
    return b"".join(chunks)


def stream_to(url, write, referer=None, timeout=180, max_bytes=None, chunk=1 << 20):
    """Stream one response into ``write`` under the same limits as a download.

    Callers that assemble a file from several responses (HLS segments) need the
    transfer rules without the single-file validation step; the assembled result
    is validated by the caller before publication.
    """
    limit = _parameters(timeout, max_bytes, DEFAULT_DOWNLOAD_LIMIT, chunk)
    validate_url(url)
    with _open(url, referer, timeout) as response:
        return _transfer(response, write, limit, chunk)


def download_atomic(url, dest, referer=None, timeout=180, max_bytes=None, chunk=1 << 20):
    """Stream, fsync, verify by destination suffix, then atomically replace.

    Failure preserves any existing destination and removes staging files. The
    caller holds the directory lock from allocation through manifest publication.
    Default limit: 512 MiB. Signed URL errors must be displayed via safe_error.
    """
    from .verify import check_file

    limit = _parameters(timeout, max_bytes, DEFAULT_DOWNLOAD_LIMIT, chunk)
    validate_url(url)
    directory = os.path.dirname(os.fspath(dest)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".crd-download-", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            with _open(url, referer, timeout) as response:
                total = _transfer(response, stream.write, limit, chunk)
            stream.flush()
            os.fsync(stream.fileno())
        ok, reason, _ = check_file(temporary, extension=os.path.splitext(os.fspath(dest))[1])
        if not ok:
            raise ValueError("Downloaded file failed validation: " + reason)
        os.replace(temporary, dest)
        return total
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
