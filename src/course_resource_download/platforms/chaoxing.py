"""Download permitted course documents from the Chaoxing (Xuexitong) student UI.

The adapter drives the normal student course page, reads the chapter list from
the rendered catalog, and visits each knowledge node's study page exactly like
a student click does. Resource identity is the platform ``objectid`` of an
Ananas document object; the per-account ``enc`` value is read from the page at
run time and never hardcoded. Page structure and status fields are observed
behavior, not a documented platform API. Signed download URLs are transient and
are not written to manifests; the manifest records the stable object identity.
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
    safe_name,
    summarize,
    write_manifest,
)
from .aicourse import DEFAULT_STATE, DOC_EXT, VIDEO_EXT, sync_playwright
from .knowledge import validate_cdp_endpoint

BASE_MOOC1 = "https://mooc1.chaoxing.com"
REFERER = BASE_MOOC1 + "/"
STATUS_TIMEOUT_MS = 30000

JS_FRAME_SRC = """() => {
    const frame = document.querySelector('iframe[name="frame_content-zj"]');
    return frame ? frame.src : null;
}"""

JS_CHAPTERS = """() => {
    const items = [...document.querySelectorAll('.chapter_item')].map(e => ({
        title: e.getAttribute('title'),
        clickable: !!e.getAttribute('onclick'),
        onclick: e.getAttribute('onclick'),
        label: (((e.querySelector('.catalog_name') || {}).innerText) || '').trim()
                   .split('\\n')[0] || e.getAttribute('title') || ''
    }));
    return JSON.stringify({
        items: items,
        source: typeof window.toOld === 'function' ? String(window.toOld) : '',
        mooc1: (window.ServerHost || {}).mooc1Domain || null
    });
}"""

# Same-origin fetch from the study page for the cards HTML and status JSON.
JS_FETCH = (
    """async (url) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), """
    + str(STATUS_TIMEOUT_MS)
    + """);
    try {
        const response = await fetch(url, {credentials: 'include', signal: controller.signal});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.text();
    } finally { clearTimeout(timer); }
}"""
)

TOOLD_ARGS = re.compile(r"toOld\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'")
ENC_IN_SOURCE = re.compile(r'\benc\s*=\s*"([0-9a-f]{32})"')
CPI_IN_SOURCE = re.compile(r"\bcpi\s*=\s*(\d+)")
OBJECT_ID = re.compile(r"objectid[\\\"']*\s*[:=]\s*[\\\"']*([0-9a-f]{32})", re.I)
LEGACY_OFFICE_EXT = {".ppt", ".doc", ".xls"}


def validate_course_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host == "chaoxing.com" or host.endswith(".chaoxing.com")):
        raise ValueError("--url must be an HTTPS Chaoxing course page URL")
    return url


def query_param(url, name):
    values = parse_qs(urlsplit(url).query).get(name)
    return values[0] if values else None


def parse_knowledge_id(onclick):
    """Return (courseId, knowledgeId, clazzId) from one toOld(...) call."""
    if not isinstance(onclick, str):
        return None
    match = TOOLD_ARGS.search(onclick)
    if not match:
        return None
    course_id, knowledge_id, class_id = (group.strip() for group in match.groups())
    if not (course_id.isdigit() and knowledge_id.isdigit() and class_id.isdigit()):
        return None
    return course_id, knowledge_id, class_id


def parse_toold_source(source):
    """Read the per-account enc and cpi values embedded in the page's toOld."""
    if not isinstance(source, str):
        return None, None
    enc = ENC_IN_SOURCE.search(source)
    cpi = CPI_IN_SOURCE.search(source)
    return (enc.group(1) if enc else None), (cpi.group(1) if cpi else None)


def extract_object_ids(html):
    """Collect distinct document object identities from one cards page."""
    if not isinstance(html, str):
        return []
    seen, result = set(), []
    for match in OBJECT_ID.finditer(html):
        objectid = match.group(1).lower()
        if objectid not in seen:
            seen.add(objectid)
            result.append(objectid)
    return result


def classify_status(body, include_video=False):
    """Classify one Ananas status object; the objectid is the resource identity."""
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("objectid"), str)
        or not body["objectid"].strip()
    ):
        return {"status": "failed", "error": "Malformed platform status"}
    objectid = body["objectid"].strip()
    entry = {
        "url": "objectid:" + objectid,
        "objectid": objectid,
        "name": body.get("filename"),
        "file": None,
    }
    filename = body.get("filename")
    if not isinstance(filename, str) or not filename:
        entry.update(status="failed", error="Platform status has no usable filename")
        return entry
    ext = os.path.splitext(safe_name(filename))[1].lower()
    if not body.get("download"):
        entry["status"] = "permission_denied"
        return entry
    if ext in VIDEO_EXT:
        if not include_video:
            entry["status"] = "skipped_video"
        else:
            entry.update(
                status="skipped_unsupported_format",
                error="Media validation is unsupported",
            )
        return entry
    if ext in LEGACY_OFFICE_EXT:
        entry.update(
            status="skipped_unsupported_format",
            error="Structural validation for this format is not supported",
        )
        return entry
    if ext not in DOC_EXT:
        entry.update(status="skipped_non_document", error="Filename extension is not allowed")
        return entry
    entry.update(status="discovered", file=safe_name(filename))
    return entry


def cards_url(base, course_id, class_id, cpi, knowledge_id):
    return (
        f"{base}/mooc-ans/knowledge/cards?clazzid={class_id}&courseid={course_id}"
        f"&knowledgeid={knowledge_id}&num=0&ut=s&cpi={cpi}&mooc2=1"
    )


def study_url(base, course_id, class_id, cpi, enc, knowledge_id):
    return (
        f"{base}/mycourse/studentstudy?chapterId={knowledge_id}&courseId={course_id}"
        f"&clazzid={class_id}&cpi={cpi}&enc={enc}&mooc2=1&hidetype=0&fanyaVersion=0"
    )


def status_url(base, objectid):
    return f"{base}/ananas/status/{objectid}?k=&flag=normal"


def selected_chapter(label, selections):
    """Selections are case-insensitive label prefixes; empty selects everything."""
    return not selections or any(
        label.casefold().startswith(str(selection).casefold()) for selection in selections
    )


def collect_chapters(raw, base, cpi, enc):
    """Build the visit plan; items without a usable identity are reported locked."""
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("Chapter list is empty or unrecognized")
    plan, locked = [], []
    for item in items:
        if not isinstance(item, dict):
            locked.append("Unnamed chapter")
            continue
        label = str(item.get("label") or item.get("title") or "Unnamed chapter")
        identity = parse_knowledge_id(item.get("onclick")) if item.get("clickable") else None
        if identity is None:
            locked.append(label)
            continue
        parsed_course, knowledge_id, parsed_class = identity
        plan.append(
            {
                "label": label,
                "knowledgeId": knowledge_id,
                "courseId": parsed_course,
                "classId": parsed_class,
                "study": study_url(base, parsed_course, parsed_class, cpi, enc, knowledge_id),
                "cards": cards_url(base, parsed_course, parsed_class, cpi, knowledge_id),
            }
        )
    if not plan:
        raise ValueError("No clickable chapter carries a usable identity")
    return plan, locked


def merge_chapter(entry, label):
    """Record every chapter that exposes the same object identity."""
    if not label:
        return
    chapters = entry.setdefault("chapters", [entry.get("chapter")])
    if label not in chapters:
        chapters.append(label)


def visit_chapters(page, plan, base, args, entries, store, signed, records, checkpoint=None):
    """Visit each selected knowledge node; independent failures never stop the run."""
    for chapter in plan:
        record = {
            "chapter": chapter["label"],
            "knowledgeId": chapter["knowledgeId"],
            "resourceCount": 0,
            "status": "pending",
            "reason": None,
        }
        records.append(record)
        try:
            page.goto(chapter["study"], wait_until="load", timeout=90000)
            if "passport2.chaoxing.com" in page.url:
                raise RuntimeError("Login state expired")
            html = page.evaluate(JS_FETCH, chapter["cards"])
            if not isinstance(html, str):
                raise ValueError("Malformed knowledge cards response")
            objectids = extract_object_ids(html)
            for objectid in objectids:
                if objectid in store:
                    merge_chapter(store[objectid], chapter["label"])
                    continue
                try:
                    raw = page.evaluate(JS_FETCH, status_url(base, objectid))
                    body = json.loads(raw) if isinstance(raw, str) and raw else None
                    entry = classify_status(body)
                    if entry["status"] == "discovered":
                        signed[entry["url"]] = body.get("download")
                except Exception as exc:
                    entry = {
                        "url": "objectid:" + objectid,
                        "objectid": objectid,
                        "name": None,
                        "file": None,
                        "status": "failed",
                        "error": safe_error(exc),
                    }
                entry.setdefault("chapter", chapter["label"])
                store[objectid] = entry
                entries.append(entry)
                if checkpoint:
                    checkpoint()
            record["resourceCount"] = len(objectids)
            record["status"] = "ok"
        except Exception as exc:
            record.update(status="failed", reason=safe_error(exc))
        finally:
            if checkpoint:
                checkpoint()
        print("  [{}] {}".format(record["status"], record["chapter"]))


def download_entries(entries, signed, args, index, checkpoint=None):
    """Process discovered resources; signed URLs stay out of written records."""
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
            else:
                url = signed.get(entry["url"])
                if not isinstance(url, str) or not url:
                    raise RuntimeError(
                        "Signed download URL is no longer available; re-run discovery"
                    )
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
    parser.add_argument("--url", required=True, help="Chaoxing student course (stu) page URL")
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
        "--cdp-endpoint", help="Attach to a caller-verified browser on loopback; keep it open"
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_course_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    course_id = query_param(args.url, "courseid") or ""
    class_id = query_param(args.url, "clazzid") or ""
    if not (course_id.isdigit() and class_id.isdigit()):
        parser.error("--url must contain numeric courseid and clazzid parameters")
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
    store, signed = {}, {}
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
                    HTMLMediaElement.prototype.play = function(){return Promise.reject(new Error('Document discovery disables playback'))};""")
                page.goto(args.url, wait_until="load", timeout=90000)
                if "passport2.chaoxing.com" in page.url:
                    raise RuntimeError("Login state expired")
                page.wait_for_selector(
                    'iframe[name="frame_content-zj"]', state="attached", timeout=30000
                )
                course_url = page.evaluate(JS_FRAME_SRC)
                if not isinstance(course_url, str) or not course_url:
                    raise RuntimeError("Course content frame not found on the course page")
                page.goto(course_url, wait_until="load", timeout=90000)
                if "passport2.chaoxing.com" in page.url:
                    raise RuntimeError("Login state expired")
                page.wait_for_selector(".chapter_item", state="attached", timeout=30000)
                raw = json.loads(page.evaluate(JS_CHAPTERS))
                enc, cpi = parse_toold_source(raw.get("source"))
                if not enc or not cpi:
                    raise RuntimeError("Cannot resolve the per-account course parameters")
                base = raw.get("mooc1") if isinstance(raw.get("mooc1"), str) else BASE_MOOC1
                plan, locked = collect_chapters(raw, base, cpi, enc)
                for label in locked:
                    # Catalog headings and locked nodes both lack a knowledge
                    # identity; they are surfaced, not treated as run failures.
                    records.append(
                        {
                            "chapter": label,
                            "knowledgeId": None,
                            "resourceCount": 0,
                            "status": "locked",
                            "reason": "Chapter item has no knowledge identity",
                        }
                    )
                    print("  [locked] " + label)
                selected = [item for item in plan if selected_chapter(item["label"], args.chapter)]
                for item in plan:
                    if item not in selected:
                        records.append(
                            {
                                "chapter": item["label"],
                                "knowledgeId": item["knowledgeId"],
                                "resourceCount": 0,
                                "status": "ok",
                                "reason": "Excluded by --chapter selection",
                            }
                        )
                visit_chapters(
                    page, selected, base, args, entries, store, signed, records, checkpoint
                )
            finally:
                if attached:
                    if page is not None:
                        page.close()
                else:
                    browser.close()
        if not args.list_only:
            download_entries(entries, signed, args, load_manifest_index(args.out), checkpoint)
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
