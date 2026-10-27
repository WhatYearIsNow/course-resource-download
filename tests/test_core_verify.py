"""Adversarial formats and real small documents, generated offline."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pypdf import PdfWriter

from course_resource_download import verify


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def office_parts(ext):
    main, content_type = verify.OOXML_REQUIRED[ext]
    types = f'<Types xmlns="{verify.CT_NS}"><Override PartName="/{main}" ContentType="{content_type}"/></Types>'
    rels = f'<Relationships xmlns="{verify.PKG_REL_NS}"><Relationship Id="root" Type="{verify.REL_NS}/officeDocument" Target="{main}"/></Relationships>'
    parts = {"[Content_Types].xml": types, "_rels/.rels": rels}
    if ext == ".docx":
        parts[main] = (
            f'<w:document xmlns:w="{verify.W_NS}"><w:body><w:p><w:r><w:t>Notes</w:t></w:r></w:p></w:body></w:document>'
        )
    elif ext == ".pptx":
        parts[main] = (
            f'<p:presentation xmlns:p="{verify.P_NS}" xmlns:r="{verify.REL_NS}"><p:sldIdLst><p:sldId id="256" r:id="s1"/></p:sldIdLst></p:presentation>'
        )
        parts["ppt/_rels/presentation.xml.rels"] = (
            f'<Relationships xmlns="{verify.PKG_REL_NS}"><Relationship Id="s1" Type="{verify.REL_NS}/slide" Target="slides/slide1.xml"/></Relationships>'
        )
        parts["ppt/slides/slide1.xml"] = (
            f'<p:sld xmlns:p="{verify.P_NS}"><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/></p:spTree></p:cSld></p:sld>'
        )
    else:
        parts[main] = (
            f'<workbook xmlns="{verify.S_NS}" xmlns:r="{verify.REL_NS}"><sheets><sheet name="Notes" sheetId="1" r:id="s1"/></sheets></workbook>'
        )
        parts["xl/_rels/workbook.xml.rels"] = (
            f'<Relationships xmlns="{verify.PKG_REL_NS}"><Relationship Id="s1" Type="{verify.REL_NS}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
        )
        parts["xl/worksheets/sheet1.xml"] = (
            f'<worksheet xmlns="{verify.S_NS}"><sheetData/></worksheet>'
        )
    return parts


def write_package(path, parts):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, contents in parts.items():
            package.writestr(name, contents)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def test_real_pdf_under_one_kib_passes_and_truncation_fails(self):
        path = self.out / "notes.pdf"
        data = pdf_bytes()
        self.assertLess(len(data), 1024)
        path.write_bytes(data)
        self.assertEqual(verify.check_file(path), (True, "", 1))
        for invalid in [data[:-20], b"%PDF-1.7\n" + b"x" * 5000, b"<html>login</html>"]:
            path.write_bytes(invalid)
            self.assertFalse(verify.check_file(path)[0])

    def test_encrypted_and_zero_page_pdf_are_explicit_failures(self):
        path = self.out / "notes.pdf"
        writer = PdfWriter()
        writer.write(path)
        self.assertEqual(verify.check_file(path)[2], 0)
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("password")
        writer.write(path)
        ok, reason, _ = verify.check_file(path)
        self.assertFalse(ok)
        self.assertIn("Encrypted", reason)

    def test_valid_office_documents_and_declared_slide_count(self):
        for ext in (".docx", ".pptx", ".xlsx"):
            with self.subTest(ext=ext):
                path = self.out / ("notes" + ext)
                parts = office_parts(ext)
                if ext == ".pptx":
                    parts["ppt/slides/slide99.xml"] = parts["ppt/slides/slide1.xml"]
                write_package(path, parts)
                self.assertEqual(verify.check_file(path), (True, "", 1 if ext == ".pptx" else None))

    def test_office_rejects_zip_shells_malformed_xml_and_missing_relationships(self):
        for ext in (".docx", ".pptx", ".xlsx"):
            main = verify.OOXML_REQUIRED[ext][0]
            path = self.out / ("bad" + ext)
            mutations = []
            parts = office_parts(ext)
            mutations.append({main: parts[main]})
            mutations.append({**parts, main: "<malformed>"})
            mutations.append(
                {**parts, "_rels/.rels": f'<Relationships xmlns="{verify.PKG_REL_NS}"/>'}
            )
            mutations.append(
                {**parts, "_rels/.rels": parts["_rels/.rels"].replace(main, "missing.xml")}
            )
            mutations.append({**parts, main: '<!DOCTYPE x [<!ENTITY a "danger">]><x>&a;</x>'})
            for bad in mutations:
                with self.subTest(ext=ext, parts=list(bad)):
                    write_package(path, bad)
                    self.assertFalse(verify.check_file(path)[0])

    def test_presentation_requires_real_referenced_nonempty_slide_structure(self):
        path = self.out / "bad.pptx"
        parts = office_parts(".pptx")
        cases = [
            {**parts, "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{verify.P_NS}"/>'},
            {
                **parts,
                "ppt/presentation.xml": parts["ppt/presentation.xml"].replace(
                    'r:id="s1"', 'r:id="missing"'
                ),
            },
            {
                **parts,
                "ppt/_rels/presentation.xml.rels": parts["ppt/_rels/presentation.xml.rels"].replace(
                    'Target="slides/slide1.xml"',
                    'Target="https://example.test/slide.xml" TargetMode="External"',
                ),
            },
            {**parts, "ppt/presentation.xml": f'<p:presentation xmlns:p="{verify.P_NS}"/>'},
            {
                **parts,
                "ppt/_rels/presentation.xml.rels": parts["ppt/_rels/presentation.xml.rels"].replace(
                    "slides/slide1.xml", "../../outside.xml"
                ),
            },
        ]
        for mutated in cases:
            write_package(path, mutated)
            self.assertFalse(verify.check_file(path)[0])

    def test_workbook_requires_sheet_data_and_document_requires_body(self):
        path = self.out / "bad.xlsx"
        parts = office_parts(".xlsx")
        parts["xl/worksheets/sheet1.xml"] = f'<worksheet xmlns="{verify.S_NS}"/>'
        write_package(path, parts)
        self.assertFalse(verify.check_file(path)[0])
        parts = office_parts(".docx")
        parts["word/document.xml"] = f'<w:document xmlns:w="{verify.W_NS}"/>'
        path = self.out / "bad.docx"
        write_package(path, parts)
        self.assertFalse(verify.check_file(path)[0])

    def test_invalid_relationship_ids_and_binary_dependency_references_fail(self):
        path = self.out / "bad.docx"
        parts = office_parts(".docx")
        parts["word/document.xml"] = (
            f'<w:document xmlns:w="{verify.W_NS}" xmlns:r="{verify.REL_NS}"><w:body><w:p r:embed="missing"/></w:body></w:document>'
        )
        write_package(path, parts)
        self.assertFalse(verify.check_file(path)[0])
        parts = office_parts(".pptx")
        parts["ppt/_rels/presentation.xml.rels"] = parts["ppt/_rels/presentation.xml.rels"].replace(
            "</Relationships>",
            f'<Relationship Id="s1" Type="{verify.REL_NS}/slide" Target="slides/slide1.xml"/></Relationships>',
        )
        path = self.out / "bad.pptx"
        write_package(path, parts)
        self.assertFalse(verify.check_file(path)[0])

    def test_empty_relationship_reference_is_not_dangling(self):
        # PowerPoint emits r:id="" on navigation hyperlinks without a target;
        # an empty reference names no resource and must not fail validation.
        path = self.out / "actions.pptx"
        parts = office_parts(".pptx")
        parts["ppt/slides/slide1.xml"] = (
            f'<p:sld xmlns:p="{verify.P_NS}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="{verify.REL_NS}">'
            "<p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>"
            '<p:sp><p:nvSpPr><p:cNvPr><a:hlinkClick r:id="" action="ppaction://first"/></p:cNvPr></p:nvSpPr></p:sp>'
            "</p:spTree></p:cSld></p:sld>"
        )
        write_package(path, parts)
        self.assertEqual(verify.check_file(path), (True, "", 1))

    def test_text_validates_encoding_and_login_html(self):
        path = self.out / "answer.txt"
        path.write_text("第 1 题：答案为 42。\n", encoding="utf-8-sig")
        self.assertEqual(verify.check_file(path), (True, "", None))
        for invalid in [
            b"",
            b" \n",
            b"\xff\xfe\x00",
            b"binary\0data",
            b"<!DOCTYPE html><html>login</html>",
        ]:
            path.write_bytes(invalid)
            self.assertFalse(verify.check_file(path)[0])

    def test_legacy_office_and_media_are_honestly_unsupported(self):
        for ext in (".mp4", ".m4a", ".ppt", ".doc", ".xls", ".unknown"):
            path = self.out / ("file" + ext)
            path.write_bytes(b"signature is not a structural document" * 100)
            self.assertFalse(verify.check_file(path)[0])
        self.assertFalse(verify.check_file(self.out / "missing.pdf")[0])

    def test_directory_verification_excludes_internal_reports_and_lock(self):
        (self.out / "notes.pdf").write_bytes(pdf_bytes())
        for name in (
            "_manifest.json",
            "_discovery.json",
            ".download.lock",
            ".crd-download-test.part",
        ):
            (self.out / name).write_text("internal", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(verify.main([str(self.out), "--json"]), 0)
        rows = json.loads(output.getvalue())
        self.assertEqual([row["file"] for row in rows], ["notes.pdf"])


if __name__ == "__main__":
    unittest.main()
