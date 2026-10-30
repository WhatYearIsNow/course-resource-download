"""Download permitted course materials from the Zhihuishu stuonline student UI."""

import argparse
import json
import os
import re
import time

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
from .aicourse import DEFAULT_STATE, DOC_EXT, VIDEO_EXT, resource_entry, sync_playwright

DOC_TYPES = {extension.lstrip(".") for extension in DOC_EXT} | {"word"}

JS_ROWS = """() => [...document.querySelectorAll('tr')].map(r => {
    const a = r.querySelector('a');
    return {fid: r.getAttribute('fid'), name: r.getAttribute('foldername'),
            isFile: r.classList.contains('tm-file'),
            ftype: r.getAttribute('filetype'), opid: r.getAttribute('opid'),
            candownload: a ? a.getAttribute('candownload') : null,
            onclick: a ? a.getAttribute('onclick') : null};
}).filter(x => x.name || x.fid || x.isFile)"""

JS_VIDEO = re.compile(
    r"tm_data_show_proxy\(\s*this\s*,\s*[^,]*,\s*(['\"])[^'\"]*\1\s*,\s*"
    r"(['\"])[^'\"]*\2\s*,\s*(['\"])(.*?)\3"
)

JS_DOWNLOAD = """async (args) => {
    const [id, ts] = args;
    const body = new URLSearchParams();
    body.append('dataDto.dataId', id);
    const response = await fetch('/stuonline/json/data/downloadMaterial1?time=' + ts, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                  'X-Requested-With': 'XMLHttpRequest'},
        body: body.toString(), credentials: 'include'});
    if (!response.ok) throw new Error('Download metadata HTTP ' + response.status);
    return await response.text();
}"""


def wait_for_table(page, timeout_ms=15000):
    """Wait until the AJAX-filled material table settles.

    The page renders its header row at DOMContentLoaded and fills the body
    afterwards, so waiting for any `tr` reads an empty table. Poll the data rows
    until the count stops changing; an empty folder settles at zero.
    """
    page.wait_for_selector("tr", state="attached", timeout=timeout_ms)
    deadline = time.monotonic() + timeout_ms / 1000
    previous = None
    stable = 0
    while time.monotonic() < deadline:
        count = page.evaluate("() => document.querySelectorAll('tr[fid], tr.tm-file').length")
        if count == previous:
            stable += 1
            if stable >= 2:
                return count
        else:
            previous = count
            stable = 0
        page.wait_for_timeout(250)
    return previous or 0


def open_folder(page, url, fid_path):
    """Replay the folder path from the root; validate navigation at every step."""
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    if "login.zhihuishu.com" in page.url:
        raise RuntimeError("Login state expired")
    wait_for_table(page)
    if not page.query_selector("tr"):
        raise RuntimeError("Course material table not found")
    for fid in fid_path:
        before = page.evaluate("() => JSON.stringify((" + JS_ROWS + ")())")
        opened = page.evaluate(
            """(fid) => {
            const row = [...document.querySelectorAll('tr[fid]')]
                .find(r => r.getAttribute('fid') === String(fid));
            const a = row && row.querySelector('a.teacher-file-a');
            if (!a) return false;
            openFolder(a);
            return true;
        }""",
            fid,
        )
        if not opened:
            return False
        # The new listing fills asynchronously; settle it before comparing.
        wait_for_table(page)
        # Keeping the old table visible does not prove navigation succeeded.
        # Identical parent/child listings conservatively fail this confirmation.
        page.wait_for_function(
            "(previous) => JSON.stringify((" + JS_ROWS + ")()) !== previous",
            arg=before,
            timeout=15000,
        )
        if "login.zhihuishu.com" in page.url or not page.query_selector("tr"):
            raise RuntimeError("Folder navigation failed or login expired")
    return True


def process_file(page, row, label, out, args, index, entry):
    """Resolve one resource; denied resources never reach the download API."""
    filetype = str(row.get("ftype") or "").lower().lstrip(".")
    is_video = ("." + filetype) in VIDEO_EXT
    if filetype not in DOC_TYPES:
        if is_video:
            if not args.include_video:
                entry["status"] = "skipped_video"
                return
        else:
            entry["status"] = "skipped_non_document"
            return
    # Platform semantics are intentionally inverted: 1 means student download denied.
    if str(row.get("candownload")) == "1":
        entry["status"] = "permission_denied"
        return
    if str(row.get("candownload")) != "0":
        entry.update(status="failed", error="Unknown download permission flag")
        return
    onclick = row.get("onclick") or ""
    if is_video:
        # Video rows carry the media URL itself; there is no download id.
        match = JS_VIDEO.search(onclick)
        if not match or not match.group(4):
            entry.update(status="failed", error="Cannot parse video URL")
            return
        url = match.group(4)
    else:
        match = re.search(
            r"filePreviewIframe1\(\s*(['\"]).*?\1\s*,\s*(['\"]).*?\2\s*,\s*(['\"])(.*?)\3",
            onclick,
        )
        if not match or not match.group(4):
            entry.update(status="failed", error="Cannot parse resource download id")
            return
        entry["dataId"] = match.group(4)
        if args.list_only:
            entry["status"] = "discovered"
            return
        raw = page.evaluate(JS_DOWNLOAD, [entry["dataId"], str(int(time.time() * 1000))])
        payload = json.loads(raw)
        url = payload.get("dataUrl") if isinstance(payload, dict) else None
    classified = resource_entry({"name": row.get("name"), "url": url}, args.include_video)
    entry.update(classified)
    if entry["status"] != "discovered":
        return
    if args.list_only:
        return
    entry.update(status="failed", error="Download interrupted before completion")
    prefix = safe_name(label, "") if label else ""
    filename = "{}__{}".format(prefix, entry["file"]) if prefix else entry["file"]
    destination, reused = claim_or_allocate(out, filename, url, index)
    entry["file"] = os.path.basename(destination)
    if reused and is_usable(destination):
        entry["status"] = "exists"
    else:
        entry["size"] = download_atomic(
            url, destination, referer="https://stuonline.zhihuishu.com/"
        )
        if not is_usable(destination):
            raise ValueError("Downloaded file failed validation")
        entry["status"] = "ok"
    index[url] = entry["file"]
    entry.pop("error", None)


def normalize_folder(value):
    """Parse a root-relative platform folder label, independent of local paths."""
    value = value.strip().replace("\\", "/")
    parts = [part for part in value.split("/") if part]
    if (
        not parts
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(part in {".", ".."} for part in parts)
    ):
        raise argparse.ArgumentTypeError(
            "Folder must be a nonempty root-relative path without '..'"
        )
    return "/".join(parts)


def selected_folder(label, selections):
    return not selections or any(
        label == selection or label.startswith(selection + "/") for selection in selections
    )


def selected_branch(label, selections):
    return selected_folder(label, selections) or any(
        selection.startswith(label + "/") for selection in selections
    )


def walk(
    page,
    url,
    fid_path,
    label,
    out,
    args,
    entries,
    visited,
    index,
    folders,
    checkpoint=None,
    selected_found=None,
):
    """Record all folders and resources, continuing after independent failures."""
    record = {
        "folder": label or "Root",
        "fidPath": list(fid_path),
        "status": "pending",
        "reason": None,
    }
    folders.append(record)
    selections = getattr(args, "folder", [])
    if selected_found is None:
        selected_found = set()
    if label in selections:
        selected_found.add(label)
    try:
        if not open_folder(page, url, fid_path):
            raise RuntimeError("Cannot enter folder")
        rows = page.evaluate(JS_ROWS)
        if not isinstance(rows, list):
            raise ValueError("Malformed material table")
        record["status"] = "ok"
        subfolders = []
        for row in rows:
            if not isinstance(row, dict):
                record.update(status="failed", reason="Malformed table row")
                continue
            if row.get("fid"):
                subfolders.append(row)
                continue
            if not selected_folder(label, selections):
                continue
            entry = {
                "chapter": label,
                "name": row.get("name"),
                "ftype": row.get("ftype"),
                "candownload": row.get("candownload"),
                "status": "failed",
                "error": "Resource processing interrupted",
            }
            entries.append(entry)
            try:
                process_file(page, row, label, out, args, index, entry)
                if entry["status"] != "failed":
                    entry.pop("error", None)
            except Exception as exc:
                entry.update(status="failed", error=safe_error(exc))
            finally:
                if checkpoint:
                    checkpoint()
            print("  [{}] {}".format(entry["status"], entry.get("name") or "Unnamed resource"))
        for folder in subfolders:
            key = str(folder["fid"])
            child_label = (label + "/" if label else "") + str(folder.get("name") or "Unnamed")
            if not selected_branch(child_label, selections):
                continue
            if key in visited:
                folders.append(
                    {
                        "folder": child_label,
                        "fidPath": fid_path + [folder["fid"]],
                        "status": "ok",
                        "reason": "Folder identity already visited",
                    }
                )
                continue
            visited.add(key)
            walk(
                page,
                url,
                fid_path + [folder["fid"]],
                child_label,
                out,
                args,
                entries,
                visited,
                index,
                folders,
                checkpoint,
                selected_found,
            )
    except Exception as exc:
        record.update(status="failed", reason=safe_error(exc))
        print("Folder failed: " + record["reason"])
    finally:
        if record["status"] == "pending":
            record.update(status="failed", reason="Folder traversal interrupted")
        if not fid_path:
            for selection in sorted(set(selections) - selected_found):
                folders.append(
                    {
                        "folder": selection,
                        "fidPath": [],
                        "status": "failed",
                        "reason": "Requested folder not found",
                    }
                )
        if checkpoint:
            checkpoint()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="stuonline stuIndexV2 student course URL")
    parser.add_argument("--out", required=True)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--include-video", action="store_true")
    parser.add_argument(
        "--folder",
        action="append",
        type=normalize_folder,
        default=[],
        help="Restrict to this root-relative folder and descendants; repeatable",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not os.path.isfile(args.state):
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
    entries, folders = [], []
    failed = False

    def checkpoint():
        write_manifest(args.out, entries, list_only=args.list_only, folders=folders)

    try:
        cleanup_stale_parts(args.out)
        cleanup_stale_lock_files(args.out)
        index = load_manifest_index(args.out)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--window-size=1500,950"],
            )
            try:
                context = browser.new_context(
                    storage_state=args.state,
                    locale="zh-CN",
                    viewport={"width": 1500, "height": 950},
                )
                context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>false});"
                )
                page = context.new_page()
                walk(
                    page,
                    args.url,
                    [],
                    "",
                    args.out,
                    args,
                    entries,
                    set(),
                    index,
                    folders,
                    checkpoint,
                )
            finally:
                browser.close()
    except KeyboardInterrupt:
        print("Interrupted; progress saved")
        failed = True
    except Exception as exc:
        print("Download failed: " + safe_error(exc))
        folders.append(
            {"folder": "run", "fidPath": [], "status": "failed", "reason": safe_error(exc)}
        )
        failed = True
    finally:
        try:
            if failed and not any(folder["status"] != "ok" for folder in folders):
                folders.append(
                    {
                        "folder": "run",
                        "fidPath": [],
                        "status": "failed",
                        "reason": "Run interrupted",
                    }
                )
            try:
                checkpoint()
            except Exception as exc:
                failed = True
                print("Could not save manifest: " + safe_error(exc))
        finally:
            release_lock(lock)
    counts, unfinished = summarize(entries)
    print("Statistics:", json.dumps(counts, ensure_ascii=False))
    return int(failed or bool(unfinished) or any(folder["status"] != "ok" for folder in folders))


if __name__ == "__main__":
    raise SystemExit(main())
