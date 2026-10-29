"""Offline structural validation; unsupported formats are explicit failures.

Supported: PDF (pypdf), OOXML .pptx/.docx/.xlsx, and UTF-8 .txt.
Legacy binary Office and video need dedicated validators and are not claimed as
verified merely because a signature or file extension resembles those formats.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

SUPPORTED_EXTENSIONS = frozenset({".pdf", ".pptx", ".docx", ".xlsx", ".txt"})
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STRICT_REL_NS = "http://purl.oclc.org/ooxml/officeDocument/relationships"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
STRICT_P_NS = "http://purl.oclc.org/ooxml/presentationml/main"
STRICT_W_NS = "http://purl.oclc.org/ooxml/wordprocessingml/main"
STRICT_S_NS = "http://purl.oclc.org/ooxml/spreadsheetml/main"
OOXML_REQUIRED = {
    ".pptx": (
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    ),
    ".docx": (
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ),
    ".xlsx": (
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    ),
}


def head_bytes(path, n=8):
    with open(path, "rb") as stream:
        return stream.read(n)


def check_pdf(path):
    if not head_bytes(path, 5).startswith(b"%PDF-"):
        return False, "Missing PDF header", None
    try:
        from pypdf import PdfReader

        with open(path, "rb") as stream:
            # strict rejects repairs that can hide truncated/corrupt downloads.
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                return False, "Encrypted PDF cannot be verified without a password", None
            pages = len(reader.pages)
            for page in reader.pages:
                # Force referenced page contents to be decoded, rather than
                # trusting only the catalog's page count.
                content = page.get_contents()
                if content is not None:
                    content.get_data()
    except ImportError:
        return False, "PDF validation requires pypdf", None
    except Exception:
        return False, "PDF structural parsing failed", None
    if pages <= 0:
        return False, "PDF has no pages", 0
    return True, "", pages


def _parse_xml(data):
    if len(data) > MAX_XML_BYTES:
        raise ValueError("XML member exceeds validation limit")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
        raise ValueError("DTD/entity declarations are not supported")
    return ET.fromstring(data)


def _relationship_source(member):
    if member == "_rels/.rels":
        return ""
    parent, basename = posixpath.split(member)
    if posixpath.basename(parent) != "_rels" or not basename.endswith(".rels"):
        raise ValueError("Invalid relationship part path")
    return posixpath.join(posixpath.dirname(parent), basename[:-5])


def _target(source, target):
    if not isinstance(target, str) or not target or "\\" in target:
        raise ValueError("Invalid package relationship target")
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query:
        raise ValueError("Invalid internal relationship URL")
    path = urllib.parse.unquote(parsed.path)
    if not path or "\\" in path:
        raise ValueError("Invalid internal relationship path")
    path = posixpath.normpath(
        path.lstrip("/")
        if path.startswith("/")
        else posixpath.join(posixpath.dirname(source), path)
    )
    if path == ".." or path.startswith("../"):
        raise ValueError("Relationship escapes package root")
    return path


def _namespace(tag):
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _relationship_id(element):
    return element.get("{" + REL_NS + "}id") or element.get("{" + STRICT_REL_NS + "}id")


def _require_relation(relations, rid, kind):
    relation = relations.get(rid)
    if not relation or relation[1] != kind or relation[2]:
        raise ValueError("Missing or invalid " + kind + " relationship")
    return relation[0]


def check_ooxml(path, ext):
    if ext not in OOXML_REQUIRED:
        return False, "Unsupported Office extension", None
    main, content_type = OOXML_REQUIRED[ext]
    try:
        with zipfile.ZipFile(path) as package:
            members = package.infolist()
            if len(members) > 10000 or sum(item.file_size for item in members) > MAX_PACKAGE_BYTES:
                raise ValueError("OOXML package exceeds validation limits")
            names = {item.filename for item in members}
            if len(names) != len(members):
                raise ValueError("Duplicate ZIP member names")
            for item in members:
                if item.flag_bits & 1:
                    raise ValueError("Encrypted ZIP members cannot be verified")
                if (
                    item.filename.startswith("/")
                    or "\\" in item.filename
                    or ".." in item.filename.split("/")
                ):
                    raise ValueError("Unsafe ZIP member path")
                if item.filename.endswith((".xml", ".rels")) and item.file_size > MAX_XML_BYTES:
                    raise ValueError("XML member exceeds validation limit")
            if not {"[Content_Types].xml", "_rels/.rels", main} <= names:
                raise ValueError("OOXML required package parts are missing")
            if package.testzip():
                raise ValueError("ZIP member checksum mismatch")
            roots = {
                name: _parse_xml(package.read(name))
                for name in names
                if name.endswith((".xml", ".rels"))
            }
            types = roots["[Content_Types].xml"]
            if types.tag != "{" + CT_NS + "}Types":
                raise ValueError("Invalid content types XML")
            overrides = {
                item.get("PartName"): item.get("ContentType")
                for item in types
                if item.tag == "{" + CT_NS + "}Override"
            }
            if overrides.get("/" + main) != content_type:
                raise ValueError("Main part content type does not match extension")
            relationships = {}
            for name, root in roots.items():
                if not name.endswith(".rels"):
                    continue
                if root.tag != "{" + PKG_REL_NS + "}Relationships":
                    raise ValueError("Invalid relationships XML")
                source = _relationship_source(name)
                if source and source not in names:
                    raise ValueError("Relationship source part is missing")
                mapping = {}
                for item in root:
                    if item.tag != "{" + PKG_REL_NS + "}Relationship":
                        raise ValueError("Invalid relationship element")
                    rid, relation_type = item.get("Id"), item.get("Type", "")
                    if (
                        not rid
                        or rid in mapping
                        or not relation_type.startswith(
                            (
                                REL_NS + "/",
                                STRICT_REL_NS + "/",
                                "http://schemas.microsoft.com/",
                                "http://purl.oclc.org/",
                                "http://schemas.openxmlformats.org/",
                            )
                        )
                    ):
                        raise ValueError("Invalid or duplicate relationship ID/type")
                    mode = item.get("TargetMode", "Internal")
                    if mode not in {"Internal", "External"}:
                        raise ValueError("Invalid relationship target mode")
                    external = mode == "External"
                    target = item.get("Target") if external else _target(source, item.get("Target"))
                    if not target or (not external and target not in names):
                        raise ValueError("Relationship target part is missing")
                    mapping[rid] = (target, relation_type.rsplit("/", 1)[-1], external)
                relationships[source] = mapping
            office = [
                relation
                for relation in relationships.get("", {}).values()
                if relation[1] == "officeDocument" and not relation[2]
            ]
            if len(office) != 1 or office[0][0] != main:
                raise ValueError("Package officeDocument relationship is missing or ambiguous")
            # A referenced resource must have a corresponding relationship in
            # its own part. This catches broken image/chart/slide dependencies.
            # An empty reference value (e.g. a navigation hlinkClick with no
            # target) names no resource and is not a dangling reference.
            for name, root in roots.items():
                if name.endswith(".rels"):
                    continue
                mapping = relationships.get(name, {})
                for element in root.iter():
                    for attribute, rid in element.attrib.items():
                        if (
                            _namespace(attribute) in {REL_NS, STRICT_REL_NS}
                            and rid
                            and rid not in mapping
                        ):
                            raise ValueError("XML references an absent relationship")
            root = roots[main]
            mapping = relationships.get(main, {})
            if ext == ".pptx":
                ns = _namespace(root.tag)
                if ns not in {P_NS, STRICT_P_NS} or root.tag != "{" + ns + "}presentation":
                    raise ValueError("Invalid presentation XML root")
                slide_list = root.find("{" + ns + "}sldIdLst")
                slides = []
                if slide_list is not None:
                    for item in slide_list:
                        if item.tag != "{" + ns + "}sldId":
                            raise ValueError("Invalid slide list entry")
                        target = _require_relation(mapping, _relationship_id(item), "slide")
                        if (
                            target in slides
                            or target not in roots
                            or roots[target].tag != "{" + ns + "}sld"
                        ):
                            raise ValueError("Invalid or duplicate referenced slide")
                        tree = roots[target].find("{" + ns + "}cSld/{" + ns + "}spTree")
                        if (
                            tree is None
                            or tree.find("{" + ns + "}nvGrpSpPr") is None
                            or tree.find("{" + ns + "}grpSpPr") is None
                        ):
                            raise ValueError("Slide is missing its required shape tree")
                        slides.append(target)
                if not slides:
                    return False, "Presentation has no referenced slides", 0
                return True, "", len(slides)
            if ext == ".docx":
                ns = _namespace(root.tag)
                if (
                    ns not in {W_NS, STRICT_W_NS}
                    or root.tag != "{" + ns + "}document"
                    or root.find("{" + ns + "}body") is None
                ):
                    raise ValueError("Invalid document XML/body")
                return True, "", None
            ns = _namespace(root.tag)
            if ns not in {S_NS, STRICT_S_NS} or root.tag != "{" + ns + "}workbook":
                raise ValueError("Invalid workbook XML root")
            sheets = root.find("{" + ns + "}sheets")
            if sheets is None or not len(sheets):
                raise ValueError("Workbook has no sheets")
            targets = set()
            for item in sheets:
                relation = mapping.get(_relationship_id(item))
                if (
                    item.tag != "{" + ns + "}sheet"
                    or not relation
                    or relation[1] not in {"worksheet", "chartsheet"}
                    or relation[2]
                ):
                    raise ValueError("Invalid sheet relationship")
                target, kind, _ = relation
                if (
                    target in targets
                    or target not in roots
                    or roots[target].tag != "{" + ns + "}" + kind
                ):
                    raise ValueError("Invalid or duplicate sheet part")
                required = "sheetData" if kind == "worksheet" else "drawing"
                if roots[target].find("{" + ns + "}" + required) is None:
                    raise ValueError("Sheet is missing its required " + required + " element")
                targets.add(target)
            return True, "", None
    except (
        OSError,
        ValueError,
        ET.ParseError,
        zipfile.BadZipFile,
        RuntimeError,
        KeyError,
        NotImplementedError,
    ) as exc:
        # No arbitrary XML member content or URLs in user-facing diagnostics.
        reason = str(exc) if isinstance(exc, ValueError) else "OOXML structural parsing failed"
        return False, reason[:160], None


def check_ole(path):
    return (
        False,
        "Legacy binary Office format requires a dedicated validator; convert to OOXML or PDF",
        None,
    )


def check_text(path):
    try:
        size = os.path.getsize(path)
        if size > MAX_XML_BYTES:
            return False, "Text file exceeds validation limit (16 MiB)", None
        with open(path, encoding="utf-8-sig", errors="strict") as stream:
            value = stream.read()
        if not value.strip():
            return False, "Text file is empty", None
        if any(ord(char) < 32 and char not in "\t\r\n\f" for char in value):
            return False, "Text file contains binary control bytes", None
        if re.match(r"\s*(?:<!doctype\s+html|<html(?:\s|>))", value, flags=re.I):
            return False, "HTML page masquerading as a text resource", None
        return True, "", None
    except (OSError, UnicodeError):
        return False, "Text file is not readable UTF-8", None


def check_file(path, *, extension=None):
    """Return (valid, safe diagnostic, page/slide count or None). Never size-only."""
    try:
        if not os.path.isfile(path) or os.path.islink(path):
            return False, "Not a regular local file", None
        if not os.path.getsize(path):
            return False, "Empty file", None
        ext = (extension if extension is not None else os.path.splitext(os.fspath(path))[1]).lower()
        if ext == ".pdf":
            return check_pdf(path)
        if ext in OOXML_REQUIRED:
            return check_ooxml(path, ext)
        if ext in {".ppt", ".doc", ".xls"}:
            return check_ole(path)
        if ext == ".txt":
            return check_text(path)
        if ext in {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".mp3", ".m4a"}:
            return (
                False,
                "Media validation is unsupported; video/audio downloads cannot be marked verified",
                None,
            )
        return False, "Unsupported file format: " + (ext or "no extension"), None
    except (OSError, ValueError, TypeError):
        return False, "Cannot read file for validation", None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dir")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--recursive", action="store_true")
    args = parser.parse_args(argv)
    directory = Path(args.dir)
    if not directory.is_dir():
        parser.error("Expected an existing directory")
    candidates = directory.rglob("*") if args.recursive else directory.glob("*")
    files = sorted(
        path
        for path in candidates
        if path.is_file()
        and path.name not in {"_manifest.json", "_discovery.json", ".download.lock"}
        and not path.name.endswith(".part")
    )
    rows = []
    for path in files:
        ok, reason, pages = check_file(path)
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        rows.append(
            {
                "file": str(path.relative_to(directory)),
                "size": size,
                "pages": pages,
                "ok": ok,
                "reason": reason,
            }
        )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(
                f"{'OK' if row['ok'] else 'BAD'} {row['file']}"
                + (f": {row['reason']}" if row["reason"] else "")
            )
        print(f"{len(rows)} files, {sum(not row['ok'] for row in rows)} failed")
    return 0 if rows and all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
