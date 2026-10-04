"""Turn an uploaded file into plain text. Nothing here touches the database."""

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import PurePath

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".log",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql",
    ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1",
}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}

MAX_PDF_PAGES = 300
MAX_DOCX_XML_BYTES = 30 * 1024 * 1024

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ExtractionError(ValueError):
    """The file can't be read as text (message is safe to show the user)."""


class UnsupportedFileType(ExtractionError):
    pass


@dataclass(frozen=True)
class ExtractedText:
    text: str
    kind: str  # "text" | "pdf" | "docx"


def supported_types_label() -> str:
    return "text and code files, .pdf and .docx"


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def clean_filename(filename: str) -> str:
    """Keep only the base name; drop paths and control characters."""
    name = PurePath(filename.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()

    return name[:255] or "file"


def extract_text(filename: str, data: bytes) -> ExtractedText:
    extension = PurePath(filename).suffix.lower()

    if extension not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileType(
            f"ORION can't read {extension or 'files without an extension'} "
            f"files. Supported: {supported_types_label()}."
        )

    if extension == ".pdf":
        text, kind = _extract_pdf(data), "pdf"
    elif extension == ".docx":
        text, kind = _extract_docx(data), "docx"
    else:
        text, kind = _decode_text(data), "text"

    text = normalize_text(text)

    if not text:
        raise ExtractionError("The file contains no readable text.")

    return ExtractedText(text=text, kind=kind)


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass

    if b"\x00" in data[:8192]:
        raise ExtractionError(
            "This doesn't look like a text file (it contains binary data)."
        )

    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _extract_docx(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractionError("This isn't a valid .docx file.") from exc

    with archive:
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError as exc:
            raise ExtractionError("This isn't a valid .docx file.") from exc

        if info.file_size > MAX_DOCX_XML_BYTES:
            raise ExtractionError("This document is too large to read.")

        with archive.open(info) as member:
            xml = member.read(MAX_DOCX_XML_BYTES + 1)

    if len(xml) > MAX_DOCX_XML_BYTES:
        raise ExtractionError("This document is too large to read.")

    # Refuse XML entity tricks (billion-laughs style) outright.
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ExtractionError("This document contains unsafe XML and was rejected.")

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ExtractionError("This .docx file is damaged.") from exc

    paragraphs = []

    for paragraph in root.iter(WORD_NS + "p"):
        parts = []

        for node in paragraph.iter():
            if node.tag == WORD_NS + "t":
                parts.append(node.text or "")
            elif node.tag == WORD_NS + "tab":
                parts.append("\t")
            elif node.tag in (WORD_NS + "br", WORD_NS + "cr"):
                parts.append("\n")

        paragraphs.append("".join(parts))

    return "\n".join(paragraphs)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ExtractionError(
            "PDF support needs the 'pypdf' package. Install it with: "
            "pip install pypdf"
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(data))

        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0

            if not unlocked:
                raise ExtractionError(
                    "This PDF is password-protected. Remove the password and "
                    "try again."
                )

        if len(reader.pages) > MAX_PDF_PAGES:
            raise ExtractionError(
                f"This PDF has more than {MAX_PDF_PAGES} pages, which is too "
                "many to read at once."
            )

        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("This PDF could not be read.") from exc

    text = "\n\n".join(page for page in pages if page)

    if not text.strip():
        raise ExtractionError(
            "This PDF has no selectable text (it may be a scanned image). "
            "ORION can't read scanned PDFs yet."
        )

    return text
