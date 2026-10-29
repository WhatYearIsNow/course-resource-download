"""Download authorized attachments from Zhihuishu AI courses.

The adapter observes the normal student UI. The platform's ``taskId`` in an
activity response identifies an activity; ``documentId`` identifies its task.
Those field semantics are observed behavior, not a documented platform API.
"""

import argparse
import json
import os
from urllib.parse import urlencode, urlsplit, urlunsplit

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
    safe_name,
    summarize,
    write_manifest,
)

BASE = "https://ai-smart-course-student-pro.zhihuishu.com"
DEFAULT_STATE = os.path.join(".course-resource-download", "state.json")
VIDEO_EXT = {".mp4", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg", ".mkv"}
DOC_EXT = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".txt"}
# Only containers with a structural validator may be downloaded as video.
DOWNLOADABLE_VIDEO_EXT = {".mp4", ".m4v"}


def sync_playwright():
    """Load the optional browser dependency only when actually running."""
    try:
        from playwright.sync_api import sync_playwright as factory
    except ImportError as exc:
        raise RuntimeError(
            "Browser support requires: pip install 'course-resource-download[browser]'"
        ) from exc
    return factory()


class ResponseCapture:
    """Per-run response state, including a queue to retain closely spaced replies."""

    def __init__(self):
        self.cards = None
        self.knowledge = False
        self.mapuid = None
        self.details = []
        self.errors = []

    @property
    def sequence(self):
        return len(self.details)

    def on_response(self, response):
        relevant = any(
            marker in response.url
            for marker in (
                "get-project-activity-detail",
                "get-project-task-path-cards",
                "get-course-mapUid",
                "list-knowledge-theme",
            )
        )
        if not relevant:
            return
        try:
            raw = response.text()
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Platform response must be an object")
            if "get-project-activity-detail" in response.url:
                self.details.append(raw)
            elif "get-project-task-path-cards" in response.url:
                self.cards = raw
            elif "list-knowledge-theme" in response.url:
                self.knowledge = True
            else:
                self.mapuid = (payload.get("data") or {}).get("mapUid")
        except Exception as exc:
            self.errors.append(safe_error(exc))


def detail_identity(raw):
    """Return the task document and activity identities, never an activity name."""
    try:
        data = json.loads(raw).get("data") or {}
        return data.get("documentId"), data.get("taskId")
    except (ValueError, TypeError, AttributeError):
        return None, None


def wait_new_activity(page, capture, document_id, seen, after_sequence=0, timeout_ms=15000):
    """Return a new response for this document, ignoring stale and repeated replies."""
    waited = 0
    while waited <= timeout_ms:
        for raw in capture.details[after_sequence:]:
            did, key = detail_identity(raw)
            if did is not None and str(did) == str(document_id) and key is not None:
                if str(key) not in seen:
                    return raw
        if waited >= timeout_ms:
            break
        page.wait_for_timeout(min(250, timeout_ms - waited))
        waited += 250
    return None


def harvest_detail(raw, store):
    """Keep every reported resource, including ebooks and missing document URLs."""
    payload = json.loads(raw)
    data = payload.get("data") or {}
    detail = data.get("detailData") or {}
    for knowledge in detail.get("knowledgeDetailList") or []:
        if not isinstance(knowledge, dict):
            store.append(
                {
                    "source": "activity",
                    "name": None,
                    "url": None,
                    "type": None,
                    "error": "Malformed knowledge record",
                }
            )
            continue
        for resource in knowledge.get("resourcesList") or []:
            if not isinstance(resource, dict):
                store.append(
                    {
                        "source": "activity",
                        "name": None,
                        "url": None,
                        "type": None,
                        "error": "Malformed resource record",
                    }
                )
                continue
            store.append(
                {
                    "source": "activity",
                    "documentId": data.get("documentId"),
                    "activityId": data.get("taskId"),
                    "activity": data.get("activityName"),
                    "knowledge": knowledge.get("nodeName"),
                    "resourceId": resource.get("resourcesId", resource.get("id")),
                    "name": resource.get("resourcesName"),
                    "url": resource.get("resourcesUrl"),
                    "type": resource.get("resourcesDataType"),
                }
            )


def harvest_cards(raw, store):
    for task in (json.loads(raw).get("data") or {}).get("teachTasks") or []:
        if not isinstance(task, dict):
            store.append(
                {
                    "source": "task",
                    "name": None,
                    "url": None,
                    "type": None,
                    "error": "Malformed task record",
                }
            )
            continue
        for resource in task.get("resources") or []:
            if not isinstance(resource, dict):
                store.append(
                    {
                        "source": "task",
                        "name": None,
                        "url": None,
                        "type": None,
                        "error": "Malformed resource record",
                    }
                )
                continue
            store.append(
                {
                    "source": "task",
                    "documentId": task.get("documentId"),
                    "activity": task.get("teachTaskName"),
                    "knowledge": None,
                    "resourceId": resource.get("id"),
                    "name": resource.get("fileName"),
                    "url": resource.get("fileUrl"),
                    "type": resource.get("resourcesDataType"),
                }
            )


def deduplicate_resources(resources):
    """Deduplicate usable URLs; URL-less records are independent discoveries."""
    seen, result = set(), []
    for resource in resources:
        url = resource.get("url")
        if isinstance(url, str) and url:
            if url in seen:
                continue
            seen.add(url)
        result.append(resource)
    return result


def resource_entry(resource, include_video=False):
    """Classify from the actual URL suffix, never from a platform label alone."""
    entry = {**resource, "file": None}
    if resource.get("permissionDenied") is True:
        entry["status"] = "permission_denied"
        return entry
    url = resource.get("url")
    if not url:
        if str(resource.get("type")) == "21":
            entry["status"] = "skipped_ebook"
        elif str(resource.get("type")) == "22":
            entry["status"] = "skipped_unsupported_format" if include_video else "skipped_video"
        else:
            entry.update(status="failed", error=resource.get("error") or "Missing resource URL")
        return entry
    if not isinstance(url, str):
        entry.update(status="failed", error="Resource URL must be a string")
        return entry
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Resource URL must be an absolute HTTP(S) URL")
        ext = os.path.splitext(parsed.path)[1].lower()
    except ValueError as exc:
        entry.update(status="failed", error=safe_error(exc))
        return entry
    if ext in VIDEO_EXT and not include_video:
        entry["status"] = "skipped_video"
    elif ext in {".ppt", ".doc", ".xls"} or (
        ext in VIDEO_EXT and ext not in DOWNLOADABLE_VIDEO_EXT
    ):
        entry.update(
            status="skipped_unsupported_format",
            error="Structural validation for this format is not supported",
        )
    elif ext not in DOC_EXT and ext not in VIDEO_EXT:
        entry.update(status="skipped_non_document", error="URL extension is not allowed")
    else:
        name = resource.get("name")
        filename = safe_name(
            name if isinstance(name, str) and name else os.path.basename(parsed.path)
        )
        if not filename.lower().endswith(ext):
            filename = os.path.splitext(filename)[0] + ext
        entry.update(status="discovered", file=filename)
    return entry


def download_candidates(url):
    """Return (url, variant) pairs to try, in order, for one resource.

    Video rows point at the original upload, which the CDN frequently cannot
    serve back (403 at origin, surfaced as 424). The platform's own player
    requests the ``_512`` transcode instead, so that is the same resource at a
    lower bitrate rather than a different interface. Permission failures apply
    to both, so this never widens what the account may fetch.
    """
    parsed = urlsplit(url)
    stem, ext = os.path.splitext(parsed.path)
    candidates = [(url, "original")]
    if ext.lower() in DOWNLOADABLE_VIDEO_EXT:
        transcoded = urlunsplit(parsed._replace(path=stem + "_512" + ext))
        candidates.append((transcoded, "transcoded"))
    return candidates


def download_resources(resources, args, index, entries=None, checkpoint=None):
    """Process each resource and checkpoint every terminal result."""
    if entries is None:
        entries = []
    for resource in resources:
        entry = resource_entry(resource, args.include_video)
        entries.append(entry)
        if entry["status"] != "discovered" or args.list_only:
            if checkpoint:
                checkpoint()
            continue
        entry["status"] = "failed"
        entry["error"] = "Download interrupted before completion"
        try:
            url = entry["url"]
            destination, reused = claim_or_allocate(args.out, entry["file"], url, index)
            entry["file"] = os.path.basename(destination)
            if reused and is_usable(destination):
                entry["status"] = "exists"
            else:
                last_error = None
                for candidate, variant in download_candidates(url):
                    try:
                        entry["size"] = download_atomic(candidate, destination, referer=BASE + "/")
                    except Exception as exc:  # noqa: BLE001 - reported below
                        last_error = exc
                        continue
                    if variant != "original":
                        entry["variant"] = variant
                    break
                else:
                    raise (
                        last_error
                        if last_error is not None
                        else ValueError("No download candidate")
                    )
                if not is_usable(destination):
                    raise ValueError("Downloaded file failed validation")
                entry["status"] = "ok"
            index[url] = entry["file"]
            entry.pop("error", None)
        except Exception as exc:
            entry.update(status="failed", error=safe_error(exc))
        finally:
            if checkpoint:
                checkpoint()
        print("  [{}] {}".format(entry["status"], entry.get("name") or "Unnamed resource"))
    return entries


def traverse_tasks(page, capture, tasks, args, map_uid, resources, records, checkpoint=None):
    """Traverse distinct activities, requiring the current document on every page."""
    planned = []
    for task in tasks:
        task = task if isinstance(task, dict) else {}
        rec = {
            "task": task.get("teachTaskName"),
            "documentId": task.get("documentId"),
            "expectedActivities": task.get("totalActivities") or 0,
            "visitedActivities": 0,
            "status": "pending",
            "reason": None,
        }
        records.append(rec)
        planned.append((task, rec))
    for task, rec in planned:
        try:
            document_id = rec["documentId"]
            if document_id is None or str(document_id).strip() == "":
                raise ValueError("Task has no documentId")
            total = int(rec["expectedActivities"])
            if total < 0:
                raise ValueError("Invalid totalActivities")
            rec["expectedActivities"] = total
            if total == 0:
                rec["status"] = "no_activities"
                continue
            if task.get("canClick") is False or task.get("lockReason"):
                rec.update(status="locked", reason="Task is not unlocked")
                continue
            query = urlencode(
                {"mapUid": map_uid, "documentId": document_id, "taskOrder": task.get("sort", "")}
            )
            url = f"{BASE}/projectTaskDetail/{args.course_id}/{args.class_id}?{query}"
            sequence = capture.sequence
            page.goto(url, wait_until="load", timeout=90000)
            seen = set()
            raw = wait_new_activity(page, capture, document_id, seen, sequence)
            if raw is None:
                raise ValueError("First activity response missing or belongs to another task")
            while raw is not None:
                _, key = detail_identity(raw)
                seen.add(str(key))
                harvest_detail(raw, resources)
                rec["visitedActivities"] = len(seen)
                if checkpoint:
                    checkpoint()
                if len(seen) >= total:
                    rec["status"] = "ok"
                    break
                if len(seen) >= args.max_activities:
                    rec.update(status="incomplete", reason="Activity limit reached")
                    break
                disabled = page.evaluate("""() => {
                    const e = document.querySelector('.task-progress__arrow--right');
                    return !e || e.className.includes('--disabled');
                }""")
                if disabled:
                    rec.update(
                        status="incomplete", reason="Next arrow unavailable before expected total"
                    )
                    break
                sequence = capture.sequence
                page.click(".task-progress__arrow--right", timeout=3500)
                raw = wait_new_activity(page, capture, document_id, seen, sequence)
                if raw is None:
                    rec.update(status="incomplete", reason="Activity repeated, stale, or timed out")
        except Exception as exc:
            rec.update(status="failed", reason=safe_error(exc))
        finally:
            if rec["status"] == "pending":
                rec.update(status="incomplete", reason="Task traversal interrupted")
            if checkpoint:
                checkpoint()


def course_map_uid(page, capture, tasks, explicit=None, timeout_ms=15000):
    """Wait for the independently delivered map UID only if navigation needs it."""
    if explicit:
        return explicit
    required = False
    for task in tasks:
        if not isinstance(task, dict) or task.get("canClick") is False or task.get("lockReason"):
            continue
        try:
            required = required or int(task.get("totalActivities") or 0) > 0
        except (ValueError, TypeError):
            pass  # The task traversal will record the invalid activity count.
    if not required:
        return capture.mapuid
    waited = 0
    while not capture.mapuid and waited < timeout_ms:
        page.wait_for_timeout(min(250, timeout_ms - waited))
        waited += 250
    if not capture.mapuid:
        raise RuntimeError("Missing mapUid; supply --map-uid")
    return capture.mapuid


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-id", required=True)
    parser.add_argument("--class-id", required=True)
    parser.add_argument("--map-uid")
    parser.add_argument("--out", required=True)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument(
        "--include-video",
        action="store_true",
        help="Also download .mp4/.m4v resources; other media containers stay skipped",
    )
    parser.add_argument("--max-activities", type=int, default=50)
    parser.add_argument("--layout", choices=["auto", "tasks", "knowledge"], default="auto")
    parser.add_argument(
        "--cdp-endpoint", help="Attach to a caller-verified browser on loopback; keep it open"
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_activities < 1:
        parser.error("--max-activities must be positive")
    if args.cdp_endpoint:
        from .knowledge import validate_cdp_endpoint

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
    resources, records, entries = [], [], []
    failed = False
    downloading = False

    def checkpoint():
        pending = (
            []
            if downloading
            else [resource_entry(r, args.include_video) for r in deduplicate_resources(resources)]
        )
        write_manifest(args.out, entries + pending, records, list_only=args.list_only)

    try:
        cleanup_stale_parts(args.out)
        cleanup_stale_lock_files(args.out)
        capture = ResponseCapture()
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
                    HTMLMediaElement.prototype.play = function(){return Promise.reject(new Error('Document discovery disables playback'))};""")
                page.on("response", capture.on_response)
                page.goto(
                    f"{BASE}/singleCourse/knowledgeStudy/{args.course_id}/{args.class_id}",
                    wait_until="load",
                    timeout=90000,
                )
                for _ in range(60):
                    # Give task cards their bounded response window even when
                    # a shared knowledge request arrives first.
                    if capture.cards or args.layout == "knowledge":
                        break
                    page.wait_for_timeout(250)
                if "login.zhihuishu.com" in page.url:
                    raise RuntimeError("Login state expired")
                if args.layout == "knowledge" or (
                    args.layout == "auto" and capture.knowledge and not capture.cards
                ):
                    from .knowledge import harvest_knowledge

                    harvest_knowledge(page, args, resources, records, checkpoint)
                else:
                    if not capture.cards:
                        raise RuntimeError("No task cards received; check course access")
                    tasks = (json.loads(capture.cards).get("data") or {}).get("teachTasks")
                    if not isinstance(tasks, list):
                        raise ValueError("Task cards did not contain a task list")
                    harvest_cards(capture.cards, resources)
                    map_uid = course_map_uid(page, capture, tasks, args.map_uid)
                    traverse_tasks(
                        page, capture, tasks, args, map_uid, resources, records, checkpoint
                    )
                if capture.errors:
                    records.append(
                        {
                            "task": "response capture",
                            "status": "failed",
                            "reason": "; ".join(capture.errors[:3]),
                        }
                    )
            finally:
                if attached:
                    if page is not None:
                        page.close()
                else:
                    browser.close()
        downloading = True
        download_resources(
            deduplicate_resources(resources),
            args,
            load_manifest_index(args.out),
            entries,
            checkpoint,
        )
    except KeyboardInterrupt:
        print("Interrupted; progress saved")
        failed = True
    except Exception as exc:
        print("Download failed: " + safe_error(exc))
        records.append({"task": "run", "status": "failed", "reason": safe_error(exc)})
        failed = True
    finally:
        try:
            # Interrupted traversal/downloads keep every discovered item accounted for.
            if not downloading:
                entries[:] = [
                    resource_entry(r, args.include_video) for r in deduplicate_resources(resources)
                ]
            elif len(entries) < len(deduplicate_resources(resources)):
                entries.extend(
                    resource_entry(r, args.include_video)
                    for r in deduplicate_resources(resources)[len(entries) :]
                )
            if failed and not args.list_only:
                for entry in entries:
                    if entry["status"] == "discovered":
                        entry.update(status="failed", error="Run interrupted before download")
            if failed and not any(
                record["status"] in ("failed", "incomplete") for record in records
            ):
                records.append({"task": "run", "status": "failed", "reason": "Run interrupted"})
            for record in records:
                if record["status"] == "pending":
                    record.update(
                        status="incomplete", reason="Task not visited before interruption"
                    )
            try:
                manifest = write_manifest(args.out, entries, records, list_only=args.list_only)
                print("Manifest:", manifest)
            except Exception as exc:
                failed = True
                print("Could not save manifest: " + safe_error(exc))
        finally:
            release_lock(lock)
    counts, unfinished = summarize(entries)
    print("Statistics:", json.dumps(counts, ensure_ascii=False))
    return int(
        failed
        or bool(unfinished)
        or any(record["status"] in ("failed", "incomplete") for record in records)
    )


if __name__ == "__main__":
    raise SystemExit(main())
