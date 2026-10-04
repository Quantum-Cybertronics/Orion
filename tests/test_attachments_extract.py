import io
import zipfile
from xml.sax.saxutils import escape

import pytest

from app.backend.attachments.extract import (
    ExtractionError,
    UnsupportedFileType,
    clean_filename,
    extract_text,
    normalize_text,
)


def make_docx(paragraphs, doctype=""):
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r></w:p>'
        for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>' + doctype +
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)

    return buffer.getvalue()


def make_pdf(pages):
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Kids [%s] /Count %d >>" % (
            " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages))), len(pages))).encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }

    for i, text in enumerate(pages):
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode()
        objects[4 + 2 * i] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {5 + 2 * i} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode()
        objects[5 + 2 * i] = (
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}

    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"

    xref_at = len(out)
    count = max(objects) + 1
    out += f"xref\n0 {count}\n".encode() + b"0000000000 65535 f \n"

    for number in range(1, count):
        out += f"{offsets[number]:010d} 00000 n \n".encode()

    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()

    return bytes(out)


# ------------------------------------------------------------------ text

def test_plain_text_utf8():
    result = extract_text("story.txt", "Once upon a time — café".encode("utf-8"))

    assert result.kind == "text"
    assert result.text == "Once upon a time — café"


def test_utf8_bom_is_removed():
    assert extract_text("a.txt", b"\xef\xbb\xbfhello").text == "hello"


def test_utf16_text_is_decoded():
    assert extract_text("a.txt", "hello wörld".encode("utf-16")).text == "hello wörld"


def test_windows_1252_text_is_decoded():
    assert extract_text("a.txt", "café".encode("cp1252")).text == "café"


def test_code_and_markdown_files_are_accepted():
    for name in ("script.py", "notes.md", "data.csv", "conf.yaml", "x.ps1"):
        assert extract_text(name, b"content").kind == "text"


def test_binary_data_is_rejected():
    with pytest.raises(ExtractionError, match="binary"):
        extract_text("a.txt", b"abc\x00\x01\x02" + bytes(range(256)))


def test_empty_after_cleaning_is_rejected():
    with pytest.raises(ExtractionError, match="no readable text"):
        extract_text("a.txt", b"  \n\n \t ")


def test_unsupported_extension():
    with pytest.raises(UnsupportedFileType, match=r"\.exe"):
        extract_text("setup.exe", b"MZ")

    with pytest.raises(UnsupportedFileType):
        extract_text("README", b"hi")


def test_extension_check_is_case_insensitive():
    assert extract_text("NOTES.TXT", b"hi").text == "hi"


def test_normalize_text_tidies_whitespace():
    assert normalize_text("a\r\n\r\n\r\n\r\nb  \n\x00c") == "a\n\nb\nc"


def test_clean_filename_drops_paths_and_control_chars():
    assert clean_filename("C:\\Users\\me\\story.txt") == "story.txt"
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename("a\x00b\nc.txt") == "abc.txt"
    assert clean_filename("") == "file"
    assert len(clean_filename("x" * 400 + ".txt")) == 255


# ------------------------------------------------------------------ docx

def test_docx_paragraphs_are_extracted():
    result = extract_text("story.docx", make_docx(["Chapter one", "", "It was dark & stormy."]))

    assert result.kind == "docx"
    assert result.text == "Chapter one\n\nIt was dark & stormy."


def test_docx_with_entities_is_rejected():
    bomb = '<!DOCTYPE d [<!ENTITY a "aaaa">]>'

    with pytest.raises(ExtractionError, match="unsafe"):
        extract_text("x.docx", make_docx(["hi"], doctype=bomb))


def test_not_a_zip_is_rejected():
    with pytest.raises(ExtractionError, match="valid .docx"):
        extract_text("x.docx", b"this is not a zip")


def test_zip_without_document_xml_is_rejected():
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.txt", "hi")

    with pytest.raises(ExtractionError, match="valid .docx"):
        extract_text("x.docx", buffer.getvalue())


def test_damaged_docx_xml_is_rejected():
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document><unclosed>")

    with pytest.raises(ExtractionError, match="damaged"):
        extract_text("x.docx", buffer.getvalue())


# ------------------------------------------------------------------- pdf

def test_pdf_text_is_extracted():
    pytest.importorskip("pypdf")

    result = extract_text("story.pdf", make_pdf(["Page one text", "Page two text"]))

    assert result.kind == "pdf"
    assert "Page one text" in result.text
    assert "Page two text" in result.text
    assert result.text.index("one") < result.text.index("two")


def test_pdf_garbage_is_rejected():
    pytest.importorskip("pypdf")

    with pytest.raises(ExtractionError, match="could not be read"):
        extract_text("x.pdf", b"%PDF-1.4 this is not really a pdf")


def test_pdf_without_text_is_rejected():
    pytest.importorskip("pypdf")

    with pytest.raises(ExtractionError, match="no selectable text"):
        extract_text("scan.pdf", make_pdf([""]))


def test_pdf_without_pypdf_gives_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ExtractionError, match="pip install pypdf"):
        extract_text("x.pdf", b"%PDF-1.4")
