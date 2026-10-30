"""Download permitted course documents and videos from icourse163 (中国大学MOOC).

The adapter reads the course tree from the student learn page's own JSON
endpoint, then resolves each unit's media URL the same way the page does.
Resource identity is the platform unit id (``docid:<id>`` / ``videoid:<id>``);
signed playback URLs are transient and are never written to manifests.

Video is served only as HLS, so each video is assembled into one MP4 by
``hls.download_hls``, which requires ffmpeg. Quizzes, homework and in-chapter
checks are recorded as traversal records only: the adapter never opens a quiz
route, because doing so would consume one of the learner's limited attempts.
"""

import argparse
import json
import os
import re
from urllib.parse import parse_qs, urlsplit

from ..common import (
    acquire_lock,
    claim_or_allocate,
    cleanup_stale_lock_files,
    cleanup_stale_parts,
    download_atomic,
    is_usable,
    load_manifest_index,
    release_lock,
    safe_error,
    summarize,
    write_manifest,
)
from ..hls import download_hls
from .aicourse import DEFAULT_STATE, resource_entry, sync_playwright
from .knowledge import validate_cdp_endpoint

REFERER = "https://www.icourse163.org/"
VIDEO_LIMIT_MB = 4096

# Unit content types observed on the course tree endpoint.
UNIT_VIDEO = 1
UNIT_DOCUMENT = 3
UNIT_CHECK = 5

# ``detail.type`` from the unit page, used when the media URL has no suffix.
TYPE_EXTENSION = {
    "pdf": ".pdf",
    "video": ".mp4",
    "ppt": ".ppt",
    "pptx": ".pptx",
    "doc": ".doc",
    "docx": ".docx",
    "xls": ".xls",
    "xlsx": ".xlsx",
    "txt": ".txt",
}

JS_CALL = """async ([kind, params]) => {
    const csrf = (document.cookie.match(/NTESSTUDYSI=([^;]+)/) || [])[1] || '';
    let url;
    const options = {credentials: 'include', headers: {}};
    if (kind === 'tree') {
        url = '/web/j/courseBean.getLastLearnedMocTermDto.rpc?csrfKey=' + encodeURIComponent(csrf);
        options.method = 'POST';
        options.headers['content-type'] = 'application/x-www-form-urlencoded';
        options.headers['edu-script-token'] = csrf;
        options.body = params;
    } else if (kind === 'unit') {
        url = '/front-mooc-learn/api/learn/content/unit-page?' + params;
    } else if (kind === 'token') {
        url = '/web/j/resourceRpcBean.getResourceTokenV2.rpc?csrfKey=' + encodeURIComponent(csrf);
        options.method = 'POST';
        options.headers['content-type'] = 'application/x-www-form-urlencoded';
        options.headers['edu-script-token'] = csrf;
        options.body = params;
    } else if (kind === 'vod') {
        url = 'https://vod.study.163.com/eds/api/v1/vod/video?' + params;
    } else {
        throw new Error('Unsupported platform call');
    }
    const response = await fetch(url, options);
    return await response.text();
}"""


def parse_course_url(url):
    """Return ``(courseKey, termId)`` from a course learn page URL."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (
        host == "icourse163.org" or host.endswith(".icourse163.org")
    ):
        raise ValueError("--url must be an https icourse163.org course page")
    match = re.search(r"/(?:spoc/|mooc/)?learn/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise ValueError("--url must be a course learn page containing the course key")
    term_id = (parse_qs(parsed.query).get("tid") or [""])[0]
    if not term_id.isdigit():
        raise ValueError("--url must contain a numeric tid parameter")
    return match.group(1), term_id


def selected_chapter(label, selections):
    """Selections are case-insensitive label prefixes; empty selects everything."""
    return not selections or any(
        label.casefold().startswith(str(selection).casefold()) for selection in selections
    )


def call(page, kind, params):
    """Run one same-origin platform call and return the decoded JSON body."""
    raw = page.evaluate(JS_CALL, [kind, params])
    if not isinstance(raw, str) or not raw:
        raise ValueError("Empty platform response")
    try:
        body = json.loads(raw)
    except ValueError:
        raise ValueError("Malformed platform response") from None
    if not isinstance(body, dict):
        raise ValueError("Malformed platform response")
    if body.get("code") not in (0, None):
        raise ValueError("Platform rejected the request: " + str(body.get("message") or "unknown"))
    return body


def fetch_tree(page, term_id):
    """Return the course tree; the term id is the only required parameter."""
    body = call(page, "tree", "termId=" + term_id)
    result = body.get("result")
    term = result.get("mocTermDto") if isinstance(result, dict) else None
    chapters = term.get("chapters") if isinstance(term, dict) else None
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("Course tree is empty or unrecognized")
    return chapters


def collect_units(chapters, selections):
    """Split the tree into downloadable resources and traversal records."""
    resources, records = [], []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        label = str(chapter.get("name") or "Unnamed chapter").strip()
        record = {"chapter": label, "resourceCount": 0, "status": "pending", "reason": None}
        records.append(record)
        if not selected_chapter(label, selections):
            record.update(status="ok", reason="Excluded by --chapter selection")
            continue
        assessments = list(chapter.get("quizs") or []) + list(chapter.get("homeworks") or [])
        for assessment in assessments:
            name = str((assessment or {}).get("name") or "Unnamed assessment").strip()
            records.append(
                {
                    "chapter": name,
                    "resourceCount": 0,
                    "status": "skipped",
                    "reason": "Assessment is not downloaded; no quiz route is opened",
                }
            )
        for lesson in chapter.get("lessons") or []:
            if not isinstance(lesson, dict):
                continue
            lesson_id = lesson.get("id")
            for unit in lesson.get("units") or []:
                if not isinstance(unit, dict):
                    continue
                name = str(unit.get("name") or "").strip()
                content_type = unit.get("contentType")
                content_id = unit.get("contentId")
                if content_type == UNIT_CHECK:
                    records.append(
                        {
                            "chapter": name or "Unnamed unit",
                            "resourceCount": 0,
                            "status": "skipped",
                            "reason": "In-chapter check is not downloaded",
                        }
                    )
                    continue
                if content_type not in (UNIT_VIDEO, UNIT_DOCUMENT) or content_id is None:
                    continue
                prefix = "videoid:" if content_type == UNIT_VIDEO else "docid:"
                resources.append(
                    {
                        "name": name or str(content_id),
                        "url": prefix + str(content_id),
                        # Documents are PDFs on this platform; the unit page
                        # confirms the real type and the download corrects the
                        # name if it differs.
                        "extension": ".mp4" if content_type == UNIT_VIDEO else ".pdf",
                        "unitId": unit.get("id"),
                        "lessonId": lesson_id,
                        "chapter": label,
                        "duration": unit.get("durationInSeconds"),
                    }
                )
        record["resourceCount"] = len(resources)
        record["status"] = "ok"
    return resources, records


def document_url(page, course_key, term_id, resource):
    """Resolve one document's media URL and real extension from the unit page."""
    params = (
        f"courseKey={course_key}&termId={term_id}"
        f"&lessonId={resource['lessonId']}&unitId={resource['unitId']}"
    )
    body = call(page, "unit", params)
    result = body.get("result") or {}
    detail = result.get("detail") or {}
    playback = result.get("pdfPlayback") or {}
    url = detail.get("downloadUrl") or detail.get("mediaUrl") or playback.get("resourceUrl")
    if not isinstance(url, str) or not url:
        raise ValueError("Document unit exposes no media URL")
    kind = str(detail.get("type") or "").lower()
    extension = TYPE_EXTENSION.get(kind) or os.path.splitext(urlsplit(url).path)[1].lower()
    if not extension:
        raise ValueError("Document unit exposes no usable file type")
    return url, extension


def video_playlist(page, unit_id):
    """Resolve one video's HLS playlist URL through the token and VOD endpoints."""
    token = call(
        page,
        "token",
        f"bizId={unit_id}&bizType=1&contentType=1&sign=&timestamp={_now_ms()}",
    )
    sign = (token.get("result") or {}).get("videoSignDto") or {}
    video_id, signature = sign.get("videoId"), sign.get("signature")
    if video_id is None or not signature:
        raise ValueError("Video unit exposes no playback signature")
    vod = call(page, "vod", f"videoId={video_id}&signature={signature}&clientType=1")
    videos = ((vod.get("result") or {}).get("videos")) or []
    streams = [item for item in videos if isinstance(item, dict) and item.get("videoUrl")]
    if not streams:
        raise ValueError("Video unit exposes no playable stream")
    streams.sort(key=lambda item: item.get("quality") or 0)
    return streams[-1]["videoUrl"]


def _now_ms():
    import time

    return int(time.time() * 1000)


def download_entries(page, course_key, term_id, entries, args, index, checkpoint=None):
    """Resolve and download each discovered resource; failures stay per-resource."""
    for entry in entries:
        if entry["status"] != "discovered":
            continue
        entry["status"] = "failed"
        entry["error"] = "Download interrupted before completion"
        try:
            destination, reused = claim_or_allocate(args.out, entry["file"], entry["url"], index)
            entry["file"] = os.path.basename(destination)
            if reused and is_usable(destination):
                entry["status"] = "exists"
            elif entry["url"].startswith("videoid:"):
                playlist = video_playlist(page, entry["unitId"])
                entry["size"] = download_hls(
                    playlist,
                    destination,
                    referer=REFERER,
                    max_bytes=args.max_video_mb * 1024 * 1024,
                )
                if not is_usable(destination):
                    raise ValueError("Downloaded video failed validation")
                entry["status"] = "ok"
            else:
                url, extension = document_url(page, course_key, term_id, entry)
                if entry["file"].lower().endswith(".pdf") and extension != ".pdf":
                    entry["file"] = os.path.splitext(entry["file"])[0] + extension
                    destination, reused = claim_or_allocate(
                        args.out, entry["file"], entry["url"], index
                    )
                    entry["file"] = os.path.basename(destination)
                    if reused and is_usable(destination):
                        entry["status"] = "exists"
                        index[entry["url"]] = entry["file"]
                        entry.pop("error", None)
                        continue
                entry["size"] = download_atomic(url, destination, referer=REFERER)
                if not is_usable(destination):
                    raise ValueError("Downloaded file failed validation")
                entry["status"] = "ok"
            index[entry["url"]] = entry["file"]
            entry.pop("error", None)
        except Exception as exc:
            entry.update(status="failed", error=safe_error(exc))
        finally:
            if checkpoint:
                checkpoint()
        print("  [{}] {}".format(entry["status"], entry.get("name") or "Unnamed resource"))
    return entries


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="icourse163 course learn page URL")
    parser.add_argument("--out", required=True)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--include-video", action="store_true")
    parser.add_argument(
        "--chapter",
        action="append",
        default=[],
        help="Restrict to chapters whose label starts with this prefix; repeatable",
    )
    parser.add_argument(
        "--max-video-mb",
        type=int,
        default=VIDEO_LIMIT_MB,
        help="Reject an assembled video larger than this many MiB",
    )
    parser.add_argument(
        "--cdp-endpoint", help="Attach to a caller-verified browser on loopback; keep it open"
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        course_key, term_id = parse_course_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    if args.max_video_mb <= 0:
        parser.error("--max-video-mb must be positive")
    if args.cdp_endpoint:
        try:
            validate_cdp_endpoint(args.cdp_endpoint)
        except ValueError as exc:
            parser.error(str(exc))
    if not args.cdp_endpoint and not os.path.isfile(args.state):
        print("Login state not found; run the login command first")
        return 1
    try:
        os.makedirs(args.out, exist_ok=True)
        lock = acquire_lock(args.out)
    except OSError as exc:
        print("Cannot prepare output directory: " + safe_error(exc))
        return 1
    if not lock:
        print("Output directory is in use by another download process")
        return 1
    entries, records = [], []
    failed = False

    def checkpoint():
        write_manifest(args.out, entries, records, list_only=args.list_only)

    try:
        cleanup_stale_parts(args.out)
        cleanup_stale_lock_files(args.out)
        with sync_playwright() as playwright:
            attached = bool(args.cdp_endpoint)
            if attached:
                browser = playwright.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
            else:
                browser = playwright.chromium.launch(
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--window-size=1500,950",
                    ],
                )
            page = None
            try:
                context = (
                    browser.contexts[0]
                    if attached
                    else browser.new_context(
                        storage_state=args.state,
                        locale="zh-CN",
                        viewport={"width": 1500, "height": 950},
                    )
                )
                page = context.new_page()
                # Page-scoped: never alter the user's other tabs or playback.
                page.add_init_script("""Object.defineProperty(navigator,'webdriver',{get:()=>false});
                    HTMLMediaElement.prototype.play = function(){return Promise.reject(new Error('Discovery disables playback'))};""")
                page.goto(args.url, wait_until="load", timeout=90000)
                if "passport.icourse163.org" in page.url or "/login" in page.url:
                    raise RuntimeError("Login state expired")
                chapters = fetch_tree(page, term_id)
                resources, records = collect_units(chapters, args.chapter)
                for resource in resources:
                    entry = resource_entry(resource, args.include_video)
                    entries.append(entry)
                    if checkpoint:
                        checkpoint()
                print(f"  [discovered] {len(entries)} resources")
                if not args.list_only:
                    download_entries(
                        page,
                        course_key,
                        term_id,
                        entries,
                        args,
                        load_manifest_index(args.out),
                        checkpoint,
                    )
            finally:
                if attached:
                    if page is not None:
                        page.close()
                else:
                    browser.close()
    except KeyboardInterrupt:
        print("Interrupted; progress saved")
        failed = True
    except Exception as exc:
        print("Download failed: " + safe_error(exc))
        records.append({"chapter": "run", "status": "failed", "reason": safe_error(exc)})
        failed = True
    finally:
        try:
            if failed and not args.list_only:
                for entry in entries:
                    if entry["status"] == "discovered":
                        entry.update(status="failed", error="Run interrupted before download")
            if failed and not any(
                record["status"] in ("failed", "incomplete") for record in records
            ):
                records.append({"chapter": "run", "status": "failed", "reason": "Run interrupted"})
            write_manifest(args.out, entries, records, list_only=args.list_only)
        finally:
            release_lock(lock)
    counts, has_failures = summarize(entries)
    print("Summary: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    return 1 if has_failures or failed else 0
