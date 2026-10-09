#!/usr/bin/env python3
"""Tabular input for CRM imports: CSV, XLSX and API JSON, plus value normalization.

Readers return a Table with headers, rows and notes about everything that was
detected. Rows carry their position in the file: the physical start line of a
CSV record, the sheet row of an XLSX cell, the item number of a JSON object.
Nothing is decided silently: an encoding fallback, the delimiter, skipped empty
rows and Excel error values are all reported. Legacy .xls workbooks and other
binary formats are refused with a request to save the data as CSV or XLSX.

Typed values stay typed: XLSX numbers arrive as int or Decimal, XLSX dates as
date or datetime, JSON numbers as int or Decimal. Only text needs locale rules.
The normalizers turn German and English spreadsheet conventions into the
canonical values of the CRM core (dot decimals, ISO dates, true/false). An
ambiguous value such as 01/02/2024 without a declared date order is an error,
never a guess.

The module also holds the column vocabulary of common CRM exports (standard field labels and the
composite subfield labels used by common CRM exports),
which the importer and the exporter share.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

from crm_contract import CURRENCY_CODE_RE, INSTANT_RE, format_instant, parse_instant

ZWJ = "\u200d"
CSV_DANGEROUS = re.compile(r"^[=+\-@\t\r]")
NUMERIC_LIKE = re.compile(r"^[+\-]?[0-9 ().,/\-]*$")


class TabularError(ValueError):
    """The file cannot be read as a table; the message says what the user can do."""


class ValueProblem(ValueError):
    """One value cannot be normalized; ``rule`` is a stable name for the row report."""

    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True)
class CellError:
    """A cell that carries no usable value, such as an Excel error (#N/A)."""

    rule: str
    message: str


@dataclass
class Row:
    number: int
    cells: dict[str, Any]


@dataclass
class Table:
    format: str
    name: str
    sha256: str
    size: int
    headers: list[str]
    rows: list[Row]
    row_unit: str
    encoding: Optional[str] = None
    delimiter: Optional[str] = None
    sheet: Optional[str] = None
    sheets: list[str] = field(default_factory=list)
    date_system: Optional[str] = None
    json_root: Optional[str] = None
    empty_rows: int = 0
    notes: list[dict[str, Any]] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)

    def note(self, rule: str, message: str) -> None:
        self.notes.append({"rule": rule, "message": message})

    def summary(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": self.name,
            "format": self.format,
            "bytes": self.size,
            "sha256": self.sha256,
            "rows": len(self.rows),
            "row_unit": self.row_unit,
            "columns": len(self.headers),
        }
        for key in ("encoding", "delimiter", "sheet", "date_system", "json_root"):
            value = getattr(self, key)
            if value is not None:
                info[key] = "TAB" if key == "delimiter" and value == "\t" else value
        if self.sheets:
            info["sheets"] = self.sheets
        if self.empty_rows:
            info["empty_rows_skipped"] = self.empty_rows
        return info


# ---------------------------------------------------------------------------
# format detection


BINARY_SIGNATURES = (
    (b"%PDF", "PDF"),
    (b"\x89PNG", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF8", "GIF"),
    (b"\x1f\x8b", "GZIP"),
    (b"7z\xbc\xaf", "7-Zip"),
    (b"Rar!", "RAR"),
    (b"{\\rtf", "RTF"),
)
OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def read_table(path: Path, *, encoding: Optional[str] = None, sheet: Optional[str] = None,
               delimiter: Optional[str] = None, name: Optional[str] = None) -> Table:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise TabularError(f"Datei nicht lesbar: {exc.strerror or exc}") from exc
    return parse_table(data, name or Path(path).name, encoding=encoding, sheet=sheet, delimiter=delimiter)


def parse_table(data: bytes, name: str, *, encoding: Optional[str] = None, sheet: Optional[str] = None,
                delimiter: Optional[str] = None) -> Table:
    if not data.strip():
        raise TabularError("Die Datei ist leer.")
    suffix = Path(name).suffix.lower()
    if data.startswith(OLE2_SIGNATURE):
        raise TabularError(
            "Die Datei ist eine Excel-Arbeitsmappe im alten Binärformat .xls (oder ein anderes OLE2-Dokument). "
            "Dieses Format kann ohne Zusatzbibliotheken nicht gelesen werden. Bitte in Excel 'Speichern unter' "
            "wählen und als 'CSV UTF-8 (durch Trennzeichen getrennt)' oder als 'Excel-Arbeitsmappe (.xlsx)' speichern."
        )
    if data.startswith(b"PK\x03\x04"):
        return _parse_zip(data, name, sheet)
    for signature, label in BINARY_SIGNATURES:
        if data.startswith(signature):
            raise TabularError(f"Die Datei ist ein {label}-Dokument, keine Tabelle. Bitte die Daten als CSV oder XLSX bereitstellen.")
    is_utf16 = data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE)
    if b"\x00" in data[:8192] and not is_utf16 and not (encoding or "").lower().startswith("utf-16"):
        raise TabularError("Die Datei enthält Binärdaten und ist keine Textdatei. Bitte die Daten als CSV oder XLSX bereitstellen.")
    text, used, note = decode_text(data, encoding)
    stripped = text.lstrip()
    if suffix == ".json" or (suffix not in {".csv", ".tsv", ".txt", ".xls"} and stripped[:1] in {"[", "{"}):
        table = parse_json_table(text, name, data)
        table.encoding = used
        return table
    if stripped[:1] == "<":
        raise TabularError("Die Datei enthält HTML oder XML statt einer Tabelle (manche Systeme speichern HTML mit der Endung .xls). Bitte als CSV oder XLSX exportieren.")
    table = parse_csv_text(text, name, data, delimiter=delimiter)
    table.encoding = used
    if note:
        table.note("encoding-fallback", note)
    if suffix == ".xls":
        table.note("xls-text", "Die Datei hat die Endung .xls, enthält aber Text; sie wurde als CSV gelesen.")
    return table


def decode_text(data: bytes, encoding: Optional[str] = None) -> tuple[str, str, Optional[str]]:
    """Return (text, encoding used, note); cp1252 only when UTF-8 fails, and then with a note."""
    if encoding:
        wanted = encoding.strip().lower().replace("_", "-")
        if wanted in {"utf-8", "utf8"}:
            wanted = "utf-8-sig"
        try:
            text = data.decode(wanted)
        except LookupError as exc:
            raise TabularError(f"Unbekannte Kodierung {encoding!r}.") from exc
        except UnicodeDecodeError as exc:
            raise TabularError(f"Die Datei passt nicht zur Kodierung {encoding}: Byte {exc.start} ist ungültig.") from exc
        return text.lstrip("\ufeff"), encoding, None
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        try:
            return data.decode("utf-16").lstrip("\ufeff"), "utf-16", None
        except UnicodeDecodeError as exc:
            raise TabularError(f"Ungültige UTF-16-Datei (Byte {exc.start}).") from exc
    try:
        text = data.decode("utf-8-sig")
        return text, "utf-8-sig" if data.startswith(codecs.BOM_UTF8) else "utf-8", None
    except UnicodeDecodeError as exc:
        position = exc.start
    try:
        text = data.decode("cp1252")
    except UnicodeDecodeError as exc:
        raise TabularError(
            "Die Datei ist weder UTF-8 noch Windows-1252. Bitte die Kodierung mit --encoding angeben "
            "(zum Beispiel latin-1 oder utf-16) oder als CSV UTF-8 speichern."
        ) from exc
    note = (
        f"Die Datei ist kein gültiges UTF-8 (erstes ungültiges Byte an Position {position}). Sie wurde als "
        "Windows-1252 (cp1252) gelesen, das Format von Excel 'CSV (Trennzeichen-getrennt)'. Bitte Umlaute in den "
        "Beispielzeilen prüfen; mit --encoding lässt sich eine andere Kodierung erzwingen."
    )
    return text, "cp1252", note


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# CSV


CSV_DELIMITERS = (";", ",", "\t", "|")
DELIMITER_NAMES = {";": "Semikolon", ",": "Komma", "\t": "Tabulator", "|": "senkrechter Strich"}


def _widths(sample: str, delimiter: str, limit: int = 50) -> list[int]:
    widths = []
    try:
        for record in csv.reader(io.StringIO(sample, newline=""), delimiter=delimiter):
            if not any(cell.strip() for cell in record):
                continue
            widths.append(len(record))
            if len(widths) >= limit:
                break
    except csv.Error:
        pass
    return widths


def _score(sample: str, delimiter: str) -> tuple[int, float, int]:
    widths = _widths(sample, delimiter)
    if not widths:
        return (0, 0.0, 0)
    header = widths[0]
    body = widths[1:] or [header]
    consistent = sum(1 for width in body if width == header) / len(body)
    return (1 if header > 1 else 0, consistent, header)


def detect_delimiter(text: str) -> tuple[str, str]:
    """Choose the delimiter among ; , TAB | with csv.Sniffer, checked by column consistency."""
    sample = text[:65536]
    sniffed = None
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters="".join(CSV_DELIMITERS)).delimiter
    except csv.Error:
        sniffed = None
    scores = {delimiter: _score(sample, delimiter) for delimiter in CSV_DELIMITERS}
    best = max(CSV_DELIMITERS, key=lambda item: scores[item])
    if sniffed in scores and scores[sniffed][0] == 1 and scores[sniffed][1] >= 0.9:
        return sniffed, "csv.Sniffer"
    if scores[best][0] == 1:
        return best, "Spaltenzahl je Zeile" if sniffed is None else f"Spaltenzahl je Zeile (Sniffer schlug {DELIMITER_NAMES.get(sniffed, sniffed)} vor)"
    return ",", "nur eine Spalte erkannt"


def _unique_headers(raw: list[str], table: Table, labels: Optional[list[str]] = None) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(raw):
        header = str(value or "").replace("\ufeff", "").strip()
        if not header:
            position = labels[index] if labels else str(index + 1)
            header = f"(Spalte {position})"
            table.note("empty-header", f"Spalte {position} hat keine Überschrift und heißt im Bericht {header}.")
        key = header.casefold()
        if key in seen:
            seen[key] += 1
            renamed = f"{header} ({seen[key]})"
            table.note("duplicate-header", f"Die Überschrift {header!r} kommt mehrfach vor; die weitere Spalte heißt {renamed!r}.")
            header = renamed
        else:
            seen[key] = 1
        headers.append(header)
    return headers


def _unterminated_quote(text: str, delimiter: str) -> bool:
    """The tolerant reader swallows the rest of a file after an unclosed quote; the strict one notices."""
    try:
        for _record in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True):
            pass
    except csv.Error as exc:
        return "unexpected end of data" in str(exc)
    return False


def parse_csv_text(text: str, name: str, data: bytes, *, delimiter: Optional[str] = None) -> Table:
    table = Table("csv", name, file_sha256(data), len(data), [], [], "line")
    first_line = 1
    match = re.match(r"^sep=(.)\r?\n", text)
    if match:
        delimiter = delimiter or match.group(1)
        text = text[match.end():]
        first_line = 2
        table.note("sep-line", f"Die erste Zeile legt das Trennzeichen fest (sep={match.group(1)}).")
    if delimiter:
        delimiter = "\t" if delimiter.lower() in {"tab", "\\t"} else delimiter
        if len(delimiter) != 1:
            raise TabularError("--delimiter muss genau ein Zeichen sein (oder 'tab').")
        reason = "vorgegeben"
    else:
        delimiter, reason = detect_delimiter(text)
    table.delimiter = delimiter
    table.note("delimiter", f"Trennzeichen {DELIMITER_NAMES.get(delimiter, delimiter)} ({reason}).")
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(max(previous_limit, 64 * 1024 * 1024))
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        start = first_line
        header_raw: Optional[list[str]] = None
        short_rows = 0
        for record in reader:
            line = start
            start = reader.line_num + first_line
            if not any(cell.strip() for cell in record):
                if header_raw is not None:
                    table.empty_rows += 1
                continue
            if header_raw is None:
                header_raw = record
                table.headers = _unique_headers(record, table)
                continue
            cells: dict[str, Any] = {}
            for index, header in enumerate(table.headers):
                cells[header] = record[index] if index < len(record) else ""
            if len(record) < len(table.headers):
                short_rows += 1
            extra = [cell for cell in record[len(table.headers):] if cell.strip()]
            if extra:
                table.problems.append({
                    "row": line, "column": None, "rule": "extra-cells",
                    "message": f"Die Zeile hat {len(record)} Zellen, aber nur {len(table.headers)} Überschriften; überzählige Werte werden nicht gelesen.",
                })
            table.rows.append(Row(line, cells))
    except csv.Error as exc:
        raise TabularError(f"CSV nicht lesbar ab Zeile {reader.line_num}: {exc}") from exc
    finally:
        csv.field_size_limit(previous_limit)
    if header_raw is None:
        raise TabularError("Die Datei enthält keine Überschriftenzeile.")
    if table.rows and any("\n" in cell or "\r" in cell for cell in table.rows[-1].cells.values()) and _unterminated_quote(text, delimiter):
        raise TabularError(
            f"Ein Anführungszeichen ab Zeile {table.rows[-1].number} wird bis zum Dateiende nicht geschlossen; "
            "alle folgenden Zeilen würden zu einer Zelle. Bitte die Datei prüfen."
        )
    if short_rows:
        table.note("short-rows", f"{short_rows} Zeilen haben weniger Zellen als Überschriften; fehlende Zellen gelten als leer.")
    if table.empty_rows:
        table.note("empty-rows", f"{table.empty_rows} leere Zeilen übersprungen.")
    return table


# ---------------------------------------------------------------------------
# XLSX (zipfile + ElementTree, no third-party packages)


XLSX_MAX_PART = 256 * 1024 * 1024
XLSX_MAX_TOTAL = 512 * 1024 * 1024
BUILTIN_DATE_FORMATS = {
    14: "date", 15: "date", 16: "date", 17: "date",
    18: "time", 19: "time", 20: "time", 21: "time", 22: "datetime",
    45: "time", 46: "duration", 47: "time",
}
OOXML_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr(element: ET.Element, name: str) -> Optional[str]:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


def _unescape(text: str) -> str:
    return OOXML_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)


def _column_index(reference: str) -> int:
    letters = re.match(r"^([A-Za-z]+)", reference or "")
    if not letters:
        return -1
    index = 0
    for character in letters.group(1).upper():
        index = index * 26 + (ord(character) - 64)
    return index - 1


def column_letters(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


class _Workbook:
    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.names = set(archive.namelist())

    def part(self, name: str) -> bytes:
        if name not in self.names:
            raise TabularError(f"Die Arbeitsmappe ist unvollständig: {name} fehlt.")
        info = self.archive.getinfo(name)
        if info.file_size > XLSX_MAX_PART:
            raise TabularError(f"Der Teil {name} ist zu groß ({info.file_size} Bytes).")
        content = self.archive.read(name)
        head = content[:4096].upper()
        if b"<!DOCTYPE" in head or b"<!ENTITY" in content.upper():
            raise TabularError("Die Arbeitsmappe enthält eine XML-DTD; sie wird aus Sicherheitsgründen nicht gelesen. Bitte als CSV speichern.")
        return content

    def xml(self, name: str) -> ET.Element:
        try:
            return ET.fromstring(self.part(name))
        except ET.ParseError as exc:
            raise TabularError(f"Die Arbeitsmappe ist beschädigt ({name}: {exc}).") from exc

    def relationships(self, part: str) -> dict[str, tuple[str, str]]:
        directory, filename = posixpath.split(part)
        rels = posixpath.join(directory, "_rels", filename + ".rels")
        if rels not in self.names:
            return {}
        found = {}
        for relation in self.xml(rels):
            target = relation.get("Target") or ""
            if relation.get("TargetMode") == "External" or not target:
                continue
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(directory, target))
            found[relation.get("Id") or ""] = (path, relation.get("Type") or "")
        return found


def _format_kind(code: str) -> Optional[str]:
    """date, datetime, time or None for a custom number format code."""
    section = code.split(";")[0]
    section = re.sub(r'"[^"]*"', "", section)
    section = re.sub(r"\\.|_.|\*.", "", section)
    section = re.sub(r"\[(?!(?:h+|m+|s+)\])[^\]]*\]", "", section, flags=re.IGNORECASE)
    lowered = section.lower().replace("general", "")
    has_date = "d" in lowered or "y" in lowered
    has_time = "h" in lowered or "s" in lowered
    if not has_date and "m" in lowered and not has_time:
        has_date = True
    if has_date and has_time:
        return "datetime"
    if has_date:
        return "date"
    if has_time:
        return "duration" if re.search(r"\[h+\]", lowered) else "time"
    return None


def excel_number(text: str) -> Any:
    """An Excel number as int or Decimal, rounded to Excel's 15 significant digits."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return text
    if not value.is_finite():
        return text
    if value == value.to_integral_value() and abs(value) < Decimal(10) ** 15:
        return int(value)
    return Context(prec=15, rounding=ROUND_HALF_EVEN).plus(value).normalize()


def excel_serial(text: str, kind: str, date1904: bool) -> Any:
    """Convert an Excel serial number to date, datetime, time or a duration text."""
    try:
        serial = Decimal(text.strip())
    except InvalidOperation as exc:
        raise ValueProblem("excel-date", f"{text!r} ist keine Excel-Seriennummer") from exc
    if serial < 0:
        raise ValueProblem("excel-date", "negative Excel-Seriennummer")
    days = int(serial)
    seconds = int(((serial - days) * 86400).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if seconds >= 86400:
        days, seconds = days + 1, seconds - 86400
    clock = time(seconds // 3600, seconds % 3600 // 60, seconds % 60)
    if kind == "duration":
        hours = days * 24 + clock.hour
        return f"{hours}:{clock.minute:02d}:{clock.second:02d}"
    if kind == "time":
        return clock
    try:
        if date1904:
            day = date(1904, 1, 1) + timedelta(days=days)
        else:
            if days == 0:
                raise ValueProblem("excel-date", "Seriennummer 0 ist kein Kalenderdatum (1900-Datumssystem)")
            if days == 60:
                raise ValueProblem("excel-date", "Seriennummer 60 ist der 29.02.1900, den es nicht gibt (1900-Datumssystem)")
            day = (date(1899, 12, 31) if days < 60 else date(1899, 12, 30)) + timedelta(days=days)
    except OverflowError as exc:
        raise ValueProblem("excel-date", f"Seriennummer {text.strip()} liegt außerhalb des Kalenders") from exc
    if kind == "date":
        return day
    return datetime.combine(day, clock)


def _rich_text(element: ET.Element) -> str:
    parts = []
    for child in element:
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            for run in child:
                if _local(run.tag) == "t":
                    parts.append(run.text or "")
    return _unescape("".join(parts))


def _parse_zip(data: bytes, name: str, sheet: Optional[str]) -> Table:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TabularError("Die Datei ist ein beschädigtes ZIP-Archiv und keine lesbare XLSX-Arbeitsmappe.") from exc
    with archive:
        names = set(archive.namelist())
        if "mimetype" in names and b"opendocument" in archive.read("mimetype"):
            raise TabularError("Die Datei ist eine OpenDocument-Tabelle (.ods). Bitte als CSV oder XLSX speichern.")
        total = sum(info.file_size for info in archive.infolist())
        if total > XLSX_MAX_TOTAL:
            raise TabularError(f"Die Arbeitsmappe ist entpackt zu groß ({total} Bytes). Bitte als CSV speichern oder aufteilen.")
        workbook = _Workbook(archive)
        main = "xl/workbook.xml"
        for path, kind in workbook.relationships("").values():
            if kind.endswith("/officeDocument"):
                main = path
        if main not in names:
            raise TabularError("Das ZIP-Archiv ist keine Excel-Arbeitsmappe (xl/workbook.xml fehlt). Bitte als CSV oder XLSX bereitstellen.")
        return _read_workbook(workbook, main, data, name, sheet)


def _read_workbook(workbook: _Workbook, main: str, data: bytes, name: str, sheet: Optional[str]) -> Table:
    root = workbook.xml(main)
    relations = workbook.relationships(main)
    date1904 = False
    sheets: list[tuple[str, str]] = []
    hidden: set[str] = set()
    for child in root:
        tag = _local(child.tag)
        if tag == "workbookPr":
            date1904 = str(child.get("date1904", "")).lower() in {"1", "true"}
        elif tag == "sheets":
            for entry in child:
                if _local(entry.tag) == "sheet":
                    target = relations.get(_attr(entry, "id") or "", ("", ""))[0]
                    sheets.append((entry.get("name") or "", target))
                    if entry.get("state") in {"hidden", "veryHidden"}:
                        hidden.add(entry.get("name") or "")
    if not sheets:
        raise TabularError("Die Arbeitsmappe enthält kein Tabellenblatt.")
    table = Table("xlsx", name, file_sha256(data), len(data), [], [], "row")
    table.sheets = [sheet_name for sheet_name, _ in sheets]
    table.date_system = "1904" if date1904 else "1900"
    if sheet is None:
        visible = [entry for entry in sheets if entry[0] not in hidden] or sheets
        chosen = visible[0]
        if len(sheets) > 1:
            table.note("first-sheet", f"Die Arbeitsmappe hat {len(sheets)} Blätter; gelesen wurde das erste sichtbare ({chosen[0]!r}). Mit --sheet lässt sich ein anderes wählen.")
    else:
        matches = [entry for entry in sheets if entry[0] == sheet] or [entry for entry in sheets if entry[0].casefold() == sheet.casefold()]
        if not matches and sheet.isdigit() and 1 <= int(sheet) <= len(sheets):
            matches = [sheets[int(sheet) - 1]]
        if not matches:
            raise TabularError(f"Blatt {sheet!r} nicht gefunden; vorhanden: {', '.join(item[0] for item in sheets)}.")
        chosen = matches[0]
    table.sheet = chosen[0]
    if not chosen[1]:
        raise TabularError(f"Das Blatt {chosen[0]!r} hat keinen Inhalt in der Arbeitsmappe.")
    shared: list[str] = []
    styles_part = None
    shared_part = None
    for path, kind in relations.values():
        if kind.endswith("/sharedStrings"):
            shared_part = path
        elif kind.endswith("/styles"):
            styles_part = path
    shared_part = shared_part or ("xl/sharedStrings.xml" if "xl/sharedStrings.xml" in workbook.names else None)
    styles_part = styles_part or ("xl/styles.xml" if "xl/styles.xml" in workbook.names else None)
    if shared_part:
        for item in workbook.xml(shared_part):
            if _local(item.tag) == "si":
                shared.append(_rich_text(item))
    style_kinds: list[Optional[str]] = []
    if styles_part:
        styles = workbook.xml(styles_part)
        custom = {}
        for element in styles.iter():
            if _local(element.tag) == "numFmt" and (element.get("numFmtId") or "").isdigit():
                custom[int(element.get("numFmtId"))] = element.get("formatCode") or ""
        for child in styles:
            if _local(child.tag) == "cellXfs":
                for xf in child:
                    if _local(xf.tag) != "xf":
                        continue
                    number_format = int(xf.get("numFmtId", "0") or 0)
                    if number_format in custom:
                        style_kinds.append(_format_kind(custom[number_format]))
                    else:
                        style_kinds.append(BUILTIN_DATE_FORMATS.get(number_format))
    raw_rows = _read_sheet(workbook.part(chosen[1]), shared, style_kinds, date1904, table)
    _rows_from_cells(raw_rows, table)
    return table


def _read_sheet(content: bytes, shared: list[str], style_kinds: list[Optional[str]], date1904: bool, table: Table) -> list[tuple[int, dict[int, Any]]]:
    rows: list[tuple[int, dict[int, Any]]] = []
    pending: dict[int, Any] = {}
    last_row = 0
    last_column = -1
    try:
        for _event, element in ET.iterparse(io.BytesIO(content), events=("end",)):
            tag = _local(element.tag)
            if tag == "c":
                reference = element.get("r") or ""
                column = _column_index(reference)
                if column < 0:
                    column = last_column + 1
                last_column = column
                value_element = None
                inline = None
                for child in element:
                    child_tag = _local(child.tag)
                    if child_tag == "v":
                        value_element = child
                    elif child_tag == "is":
                        inline = child
                cell_type = element.get("t", "n")
                raw = value_element.text if value_element is not None else None
                try:
                    if cell_type == "s":
                        value: Any = shared[int(raw)] if raw is not None else ""
                    elif cell_type == "inlineStr":
                        value = _rich_text(inline) if inline is not None else ""
                    elif cell_type == "str":
                        value = _unescape(raw or "")
                    elif cell_type == "b":
                        value = None if raw is None else raw.strip() == "1"
                    elif cell_type == "e":
                        value = CellError("excel-error", f"Excel-Fehlerwert {raw or '#ERROR'}")
                    elif cell_type == "d":
                        value = raw or ""
                    elif raw is None or raw.strip() == "":
                        value = None
                    else:
                        style = int(element.get("s", "0") or 0)
                        kind = style_kinds[style] if 0 <= style < len(style_kinds) else None
                        value = excel_serial(raw, kind, date1904) if kind else excel_number(raw)
                except ValueProblem as problem:
                    value = CellError(problem.rule, str(problem))
                except (ValueError, IndexError):
                    value = CellError("excel-cell", "Zellwert nicht lesbar")
                pending[column] = value
                element.clear()
            elif tag == "row":
                number = int(element.get("r")) if (element.get("r") or "").isdigit() else last_row + 1
                last_row = number
                last_column = -1
                rows.append((number, pending))
                pending = {}
                element.clear()
    except ET.ParseError as exc:
        raise TabularError(f"Das Tabellenblatt ist beschädigt: {exc}") from exc
    return rows


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _rows_from_cells(raw_rows: list[tuple[int, dict[int, Any]]], table: Table) -> None:
    header_index = None
    for position, (_number, cells) in enumerate(raw_rows):
        if any(not _blank(value) for value in cells.values()):
            header_index = position
            break
    if header_index is None:
        raise TabularError(f"Das Blatt {table.sheet!r} ist leer.")
    header_cells = raw_rows[header_index][1]
    width = max(header_cells) + 1
    while width > 0 and _blank(header_cells.get(width - 1)):
        width -= 1
    raw_headers = [cell_text(header_cells.get(index)) if not isinstance(header_cells.get(index), CellError) else "" for index in range(width)]
    table.headers = _unique_headers(raw_headers, table, [column_letters(index) for index in range(width)])
    for number, cells in raw_rows[header_index + 1:]:
        if all(_blank(value) for value in cells.values()):
            table.empty_rows += 1
            continue
        row = {header: cells.get(index) for index, header in enumerate(table.headers)}
        extra = [index for index, value in cells.items() if index >= width and not _blank(value)]
        if extra:
            table.problems.append({
                "row": number, "column": None, "rule": "extra-cells",
                "message": f"Zellen in Spalte {', '.join(column_letters(index) for index in extra)} haben keine Überschrift und werden nicht gelesen.",
            })
        table.rows.append(Row(number, row))
    if table.empty_rows:
        table.note("empty-rows", f"{table.empty_rows} leere Zeilen übersprungen.")


# ---------------------------------------------------------------------------
# JSON (API form, REST list form, GraphQL connection form)


def parse_json_table(text: str, name: str, data: bytes) -> Table:
    try:
        document = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise TabularError(f"Ungültiges JSON in Zeile {exc.lineno}, Spalte {exc.colno}: {exc.msg}.") from exc
    items, root = locate_items(document)
    table = Table("json", name, file_sha256(data), len(data), [], [], "item")
    table.json_root = root
    headers: dict[str, None] = {}
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise TabularError(f"Eintrag {number} ist kein JSON-Objekt; erwartet wird eine Liste von Objekten.")
        cells = {key: value for key, value in item.items() if key != "__typename"}
        if not any(not _blank(value) for value in cells.values()):
            table.empty_rows += 1
            continue
        for key in cells:
            headers.setdefault(key, None)
        table.rows.append(Row(number, cells))
    table.headers = list(headers)
    if not table.rows:
        raise TabularError("Die JSON-Datei enthält keine Datensätze.")
    return table


def locate_items(document: Any) -> tuple[list[Any], Optional[str]]:
    """Find the record list in an array, {"data": {plural: {"edges": [...]}}}, {"data": {plural: [...]}} or {plural: [...]}."""
    if isinstance(document, list):
        return document, None
    if isinstance(document, dict):
        data = document.get("data")
        if isinstance(data, list):
            return data, None
        if isinstance(data, dict) and len(data) == 1:
            key, value = next(iter(data.items()))
            if isinstance(value, dict) and isinstance(value.get("edges"), list):
                return [edge.get("node") if isinstance(edge, dict) else edge for edge in value["edges"]], key
            if isinstance(value, list):
                return value, key
            if isinstance(value, dict):
                return [value], key
        lists = [(key, value) for key, value in document.items() if isinstance(value, list) and value and all(isinstance(item, dict) for item in value)]
        if len(lists) == 1:
            return lists[0][1], lists[0][0]
        if document and "data" not in document and not lists:
            return [document], None
    raise TabularError(
        "JSON-Form nicht erkannt. Erwartet wird eine Liste von Objekten, die REST-Form {\"data\": {\"companies\": [...]}} "
        "oder die GraphQL-Form {\"data\": {\"companies\": {\"edges\": [{\"node\": {...}}]}}}."
    )


# ---------------------------------------------------------------------------
# value helpers


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, dict)) and not value)


def strip_injection_guard(value: Any) -> Any:
    """Remove the zero-width joiner CRM exports put before =, +, -, @ in CSV cells."""
    if isinstance(value, str) and value.startswith(ZWJ) and CSV_DANGEROUS.match(value[1:2] or ""):
        return value[1:]
    return value


def injection_guard(text: str) -> str:
    """Prefix a zero-width joiner when a cell could start a spreadsheet formula (as CRMs usually do)."""
    if text and CSV_DANGEROUS.match(text) and not NUMERIC_LIKE.match(text):
        return ZWJ + text
    return text


def decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", "0", ""} else text


def cell_text(value: Any) -> str:
    """Plain text for any reader value (used for text fields and reports)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, CellError):
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def describe_value(value: Any, limit: int = 40) -> str:
    text = cell_text(value).replace("\r\n", "⏎").replace("\n", "⏎").replace("\r", "⏎")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# booleans


TRUE_WORDS = {"true", "wahr", "ja", "yes", "y", "j", "x", "1", "✓", "✔"}
FALSE_WORDS = {"false", "falsch", "nein", "no", "n", "0", "✗", "✘"}


def parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, Decimal)) and value in (0, 1):
        return bool(value)
    text = cell_text(value).strip()
    if not text:
        return None
    lowered = text.casefold()
    if lowered in TRUE_WORDS:
        return True
    if lowered in FALSE_WORDS:
        return False
    raise ValueProblem("boolean", f"{describe_value(value)!r} ist kein Wahrheitswert (erwartet TRUE/FALSE, WAHR/FALSCH, ja/nein, yes/no, x, 1/0)")


# ---------------------------------------------------------------------------
# numbers


_GROUP_CHARACTERS = {" ", "\u00a0", "\u202f", "\u2009", "'", "\u2019"}
_DIGITS = re.compile(r"^[0-9]*$")


def _number_core(text: str) -> tuple[str, bool, int]:
    work = "".join(character for character in text.strip() if character not in _GROUP_CHARACTERS)
    negative = False
    if work.startswith("(") and work.endswith(")"):
        negative, work = True, work[1:-1]
    if len(work) > 1 and work.endswith("-") and work[0] not in "+-":
        negative, work = not negative, work[:-1]
    if work[:1] in {"+", "-", "\u2212"}:
        if work[0] in {"-", "\u2212"}:
            negative = not negative
        work = work[1:]
    exponent = 0
    match = re.fullmatch(r"(.+?)[eE]([+-]?[0-9]{1,4})", work)
    if match:
        work, exponent = match.group(1), int(match.group(2))
    return work, negative, exponent


def decimal_evidence(value: Any) -> Optional[str]:
    """'comma' or 'dot' when a text value proves its decimal separator, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        _code, rest = split_currency(value)
    except ValueProblem:
        rest = value
    work, _negative, _exponent = _number_core(rest)
    if not re.fullmatch(r"[0-9.,]+", work or "x"):
        return None
    has_dot, has_comma = "." in work, "," in work
    if has_dot and has_comma:
        return "comma" if work.rfind(",") > work.rfind(".") else "dot"
    for separator, means in ((",", "comma"), (".", "dot")):
        if separator in work:
            if work.count(separator) > 1:
                return "dot" if separator == "," else "comma"
            whole, fraction = work.split(separator)
            if len(fraction) != 3 or whole in {"", "0"} or len(whole) > 3:
                return means
            return None
    return None


def infer_decimal(values: Iterable[Any]) -> Optional[str]:
    """Column convention from its values: 'comma', 'dot', 'mixed' or None (no evidence)."""
    comma = dot = 0
    for value in values:
        evidence = decimal_evidence(value)
        if evidence == "comma":
            comma += 1
        elif evidence == "dot":
            dot += 1
    if comma and dot:
        return "mixed"
    return "comma" if comma else ("dot" if dot else None)


def parse_decimal(value: Any, convention: str = "dot") -> str:
    """Canonical decimal text (dot, no grouping) for text, int or Decimal input; '' for empty."""
    if isinstance(value, bool):
        raise ValueProblem("number", f"{value!r} ist keine Zahl")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueProblem("number", "keine endliche Zahl")
        return decimal_text(value)
    if isinstance(value, float):
        return decimal_text(Decimal(repr(value)))
    if not isinstance(value, str):
        raise ValueProblem("number", f"{describe_value(value)!r} ist keine Zahl")
    text = value.strip()
    if not text:
        return ""
    if "%" in text:
        raise ValueProblem("number", f"{text!r}: Prozentangaben bitte ohne % als Zahl angeben")
    work, negative, exponent = _number_core(text)
    decimal_separator, group_separator = (",", ".") if convention == "comma" else (".", ",")
    if work.count(decimal_separator) > 1:
        raise ValueProblem("number", f"{text!r} ist keine Zahl (mehrere Dezimaltrennzeichen bei Dezimal{'komma' if convention == 'comma' else 'punkt'})")
    whole, _separator, fraction = work.partition(decimal_separator)
    if group_separator in fraction:
        raise ValueProblem("number", f"{text!r} ist keine Zahl")
    if group_separator in whole:
        if not re.fullmatch(r"[0-9]{1,3}(?:%s[0-9]{3})+" % re.escape(group_separator), whole):
            raise ValueProblem("number", f"{text!r}: Tausendertrennzeichen an falscher Stelle (die Spalte nutzt Dezimal{'komma' if convention == 'comma' else 'punkt'})")
        whole = whole.replace(group_separator, "")
    if not (whole or fraction) or not _DIGITS.match(whole) or not _DIGITS.match(fraction):
        raise ValueProblem("number", f"{text!r} ist keine Zahl")
    number = Decimal(f"{whole or '0'}.{fraction or '0'}").scaleb(exponent)
    return decimal_text(-number if negative else number)


def choose_decimal(explicit: Optional[str], column: Optional[str], file_level: Optional[str],
                   delimiter: Optional[str], number_format: Optional[str]) -> tuple[str, str]:
    """(convention, reason) in the order: option, column values, file values, delimiter, wiki setting."""
    if explicit:
        return explicit, "vorgegeben (--decimal)"
    if column in {"comma", "dot"}:
        return column, "aus den Werten der Spalte"
    if file_level in {"comma", "dot"}:
        return file_level, "aus den Werten anderer Spalten der Datei"
    if delimiter == ";":
        return "comma", "Semikolon-CSV (deutsches Excel)"
    if number_format in {"DOTS_AND_COMMA", "SPACES_AND_COMMA"}:
        return "comma", "Zahlenformat des Wikis"
    return "dot", "Standard (Dezimalpunkt)"


# ---------------------------------------------------------------------------
# money


CURRENCY_SYMBOLS = {
    "€": "EUR", "EURO": "EUR", "£": "GBP", "$": "USD", "US$": "USD", "¥": "JPY", "₹": "INR", "₽": "RUB",
    "₺": "TRY", "₩": "KRW", "₪": "ILS", "zł": "PLN", "Kč": "CZK", "Fr.": "CHF", "SFr.": "CHF", "Ft": "HUF",
    "R$": "BRL", "CA$": "CAD", "C$": "CAD", "A$": "AUD",
}
AMBIGUOUS_SYMBOLS = {"kr", "kr.", "Kr", "Kr."}


def split_currency(value: str) -> tuple[Optional[str], str]:
    """(ISO code or None, number text) for texts such as '1.234,56 €' or 'EUR 1,234.56'."""
    text = value.strip()
    leading = re.match(r"^([A-Z]{3})\s*([^A-Za-z].*)$", text)
    if leading:
        return leading.group(1), leading.group(2)
    trailing = re.match(r"^(.*[^A-Za-z])\s*([A-Z]{3})$", text)
    if trailing:
        return trailing.group(2), trailing.group(1)
    for symbol in sorted(AMBIGUOUS_SYMBOLS | set(CURRENCY_SYMBOLS), key=len, reverse=True):
        for candidate in (text[: len(symbol)], text[-len(symbol):]):
            if candidate == symbol and len(text) > len(symbol):
                rest = text[len(symbol):] if text.startswith(symbol) else text[: -len(symbol)]
                if symbol in AMBIGUOUS_SYMBOLS:
                    raise ValueProblem("currency", f"{text!r}: das Währungszeichen {symbol} ist mehrdeutig (SEK, NOK, DKK, ISK); bitte den ISO-Code angeben")
                return CURRENCY_SYMBOLS[symbol], rest
    return None, text


def parse_money(value: Any, convention: str = "dot") -> tuple[str, Optional[str]]:
    """(canonical amount, ISO currency code or None) for amounts with or without currency."""
    if not isinstance(value, str):
        return parse_decimal(value, convention), None
    code, rest = split_currency(value)
    if code is not None and not CURRENCY_CODE_RE.fullmatch(code):
        raise ValueProblem("currency", f"{code!r} ist kein ISO-Währungscode")
    return parse_decimal(rest, convention), code


# ---------------------------------------------------------------------------
# dates and instants


DOT_DATE = re.compile(r"^(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})(?:[ ,]+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*Uhr)?)?$")
SEP_DATE = re.compile(r"^(\d{1,4})([/-])(\d{1,2})\2(\d{1,4})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?)?$")
ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
YMD_DOT = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$")


@dataclass
class Moment:
    day: date
    clock: Optional[time] = None
    aware: Optional[datetime] = None
    rules: tuple[str, ...] = ()


def _year(text: str, rules: list[str]) -> int:
    if len(text) == 2:
        rules.append("two-digit-year")
        number = int(text)
        return 2000 + number if number < 30 else 1900 + number
    if len(text) != 4:
        raise ValueProblem("date", "Jahreszahl muss zwei- oder vierstellig sein")
    return int(text)


def _day(year: int, month: int, day: int, text: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueProblem("date", f"{text!r} ist kein gültiges Datum") from exc


def _clock(hour: Optional[str], minute: Optional[str], second: Optional[str], meridiem: Optional[str], text: str) -> Optional[time]:
    if hour is None:
        return None
    value = int(hour)
    if meridiem:
        if not 1 <= value <= 12:
            raise ValueProblem("date", f"{text!r}: Stunde passt nicht zu AM/PM")
        value = value % 12 + (12 if meridiem.lower() == "pm" else 0)
    try:
        return time(value, int(minute), int(second or 0))
    except ValueError as exc:
        raise ValueProblem("date", f"{text!r} enthält keine gültige Uhrzeit") from exc


def parse_moment(value: Any, date_format: Optional[str] = None) -> Optional[Moment]:
    """Read a date or instant; None for empty. date_format is DMY, MDY or YMD for slash and dash dates."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return Moment(value.date(), aware=value.astimezone(timezone.utc), rules=("excel-date",))
        return Moment(value.date(), value.time(), rules=("excel-date",))
    if isinstance(value, date):
        return Moment(value, rules=("excel-date",))
    if isinstance(value, time):
        raise ValueProblem("date", "nur eine Uhrzeit, kein Datum")
    if isinstance(value, (bool, int, Decimal)):
        raise ValueProblem("date", f"{cell_text(value)!r} ist eine Zahl, kein Datum (Excel-Seriennummern werden nur aus XLSX-Zellen mit Datumsformat gelesen)")
    text = cell_text(value).strip()
    if not text:
        return None
    rules: list[str] = []
    if ISO_DATE.match(text):
        return Moment(_day(int(text[:4]), int(text[5:7]), int(text[8:10]), text), rules=("iso-date",))
    instant = INSTANT_RE.fullmatch(text)
    if instant:
        year, month, day_number, hour, minute, second, _fraction, offset = instant.groups()
        day = _day(int(year), int(month), int(day_number), text)
        if offset:
            moment = parse_instant(text)
            if moment is None:
                raise ValueProblem("date", f"{text!r} ist kein gültiger Zeitpunkt")
            return Moment(day, aware=moment, rules=("iso-instant",))
        return Moment(day, _clock(hour, minute, second, None, text), rules=("iso-local",))
    match = DOT_DATE.match(text)
    if match:
        day_text, month_text, year_text, hour, minute, second = match.groups()
        year = _year(year_text, rules)
        rules.insert(0, "date-dmy")
        return Moment(_day(year, int(month_text), int(day_text), text), _clock(hour, minute, second, None, text), rules=tuple(rules))
    match = YMD_DOT.match(text)
    if match:
        return Moment(_day(int(match.group(1)), int(match.group(2)), int(match.group(3)), text), rules=("date-ymd",))
    match = SEP_DATE.match(text)
    if match:
        first, _separator, second_part, third, hour, minute, second, meridiem = match.groups()
        clock = _clock(hour, minute, second, meridiem, text)
        if len(first) == 4:
            return Moment(_day(int(first), int(second_part), int(third), text), clock, rules=("date-ymd",))
        if len(first) > 2 or len(third) not in {2, 4}:
            raise ValueProblem("date", f"{text!r} ist kein erkanntes Datum")
        order = (date_format or "").upper()
        if order not in {"DMY", "MDY", "YMD"}:
            hint = ""
            if int(first) > 12 >= int(second_part):
                hint = "; der Wert deutet auf Tag zuerst (DMY)"
            elif int(second_part) > 12 >= int(first):
                hint = "; der Wert deutet auf Monat zuerst (MDY)"
            raise ValueProblem(
                "ambiguous-date",
                f"{text!r} ist mehrdeutig (TT/MM/JJJJ oder MM/TT/JJJJ); bitte --date-format DMY oder MDY angeben{hint}",
            )
        if order == "YMD":
            year, month, day_number = _year(first, rules), int(second_part), int(third)
        elif order == "DMY":
            year, month, day_number = _year(third, rules), int(second_part), int(first)
        else:
            year, month, day_number = _year(third, rules), int(first), int(second_part)
        rules.insert(0, f"date-{order.lower()}")
        return Moment(_day(year, month, day_number, text), clock, rules=tuple(rules))
    raise ValueProblem("date", f"{describe_value(text)!r} ist kein erkanntes Datum (erwartet JJJJ-MM-TT, TT.MM.JJJJ oder ISO 8601)")


def zone(name: Optional[str]):
    if not name or name in {"UTC", "Etc/UTC", "Z"}:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown or unavailable zone database
        return None


def moment_to_date(moment: Moment) -> str:
    return moment.day.isoformat()


def moment_to_instant(moment: Moment, time_zone: Optional[str]) -> tuple[str, Optional[str]]:
    """(value for a DATE_TIME field, extra rule). A date without time stays a date (midnight UTC in the core)."""
    if moment.aware is not None:
        return format_instant(moment.aware), None
    if moment.clock is None:
        return moment.day.isoformat(), None
    tz = zone(time_zone)
    if tz is None:
        raise ValueProblem("time-zone", f"Zeitzone {time_zone!r} ist auf diesem System nicht verfügbar; bitte --time-zone UTC oder eine IANA-Zone angeben")
    local = datetime.combine(moment.day, moment.clock).replace(tzinfo=tz)
    return format_instant(local), "local-time"


# ---------------------------------------------------------------------------
# lists


def parse_list(value: Any) -> list[Any]:
    """A JSON array text, a list, or text separated by comma, semicolon or vertical bar."""
    if isinstance(value, list):
        return [item for item in value if not is_blank(item)]
    text = cell_text(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueProblem("list", f"ungültige JSON-Liste: {exc.msg}") from exc
        if not isinstance(parsed, list):
            raise ValueProblem("list", "erwartet wird eine JSON-Liste")
        return [item for item in parsed if not is_blank(item)]
    return [part.strip() for part in re.split(r"[,;|\n]", text) if part.strip()]


def humanize(name: str) -> str:
    """Readable subfield label: addressCity -> Address City, first_name -> First Name."""
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name.strip())
    words = re.sub(r"[_\-\s]+", " ", words).strip()
    return " ".join(word[:1].upper() + word[1:].lower() if word.isupper() and len(word) > 1 else word[:1].upper() + word[1:] for word in words.split(" ") if word)


def header_key(text: str) -> str:
    """Comparison key for column headers: case, umlauts, spaces and punctuation do not count."""
    lowered = str(text).replace("\ufeff", "").strip().casefold()
    for source, replacement in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        lowered = lowered.replace(source, replacement)
    return re.sub(r"[^0-9a-z]+", "", lowered)


# ---------------------------------------------------------------------------
# Column vocabulary of common CRM exports


STANDARD_SUBFIELD_LABELS: dict[str, dict[str, str]] = {
    "CURRENCY": {"amountMicros": "Amount", "currencyCode": "Currency"},
    "EMAILS": {"primaryEmail": "Primary Email", "additionalEmails": "Additional Emails"},
    "LINKS": {"primaryLinkLabel": "Link Label", "primaryLinkUrl": "Link URL", "secondaryLinks": "Secondary Links"},
    "PHONES": {
        "primaryPhoneNumber": "Primary Phone Number", "primaryPhoneCountryCode": "Primary Phone Country Code",
        "primaryPhoneCallingCode": "Primary Phone Calling Code", "additionalPhones": "Additional Phones",
    },
    "FULL_NAME": {"firstName": "First Name", "lastName": "Last Name"},
    "ADDRESS": {
        "addressStreet1": "Address 1", "addressStreet2": "Address 2", "addressCity": "City", "addressState": "State",
        "addressCountry": "Country", "addressPostcode": "Post Code", "addressLat": "Latitude", "addressLng": "Longitude",
    },
    "RICH_TEXT": {"markdown": "Markdown", "blocknote": "BlockNote"},
}
# Some exports also name the domain columns 'Domain URL' and 'Domain Label'.
DOC_SUBFIELD_LABELS = {"LINKS": {"primaryLinkUrl": ["Domain URL", "URL"], "primaryLinkLabel": ["Domain Label", "Label"]}}
GERMAN_SUBFIELD_LABELS: dict[str, dict[str, str]] = {
    "CURRENCY": {"amountMicros": "Betrag", "currencyCode": "Währung"},
    "EMAILS": {"primaryEmail": "E-Mail", "additionalEmails": "Weitere E-Mails"},
    "LINKS": {"primaryLinkLabel": "Bezeichnung", "primaryLinkUrl": "URL", "secondaryLinks": "Weitere Links"},
    "PHONES": {
        "primaryPhoneNumber": "Telefonnummer", "primaryPhoneCountryCode": "Ländercode",
        "primaryPhoneCallingCode": "Landesvorwahl", "additionalPhones": "Weitere Telefonnummern",
    },
    "FULL_NAME": {"firstName": "Vorname", "lastName": "Nachname"},
    "ADDRESS": {
        "addressStreet1": "Straße", "addressStreet2": "Adresszusatz", "addressCity": "Ort", "addressState": "Bundesland",
        "addressCountry": "Land", "addressPostcode": "PLZ", "addressLat": "Breitengrad", "addressLng": "Längengrad",
    },
    "RICH_TEXT": {"markdown": "Markdown", "blocknote": "BlockNote"},
}
# English labels of the standard fields as CRM exports name them.
STANDARD_FIELD_LABELS: dict[str, dict[str, str]] = {
    "company": {"name": "Name", "domainName": "Domain Name", "address": "Address", "linkedinLink": "Linkedin",
                "annualRevenue": "Annual Revenue", "accountOwner": "Account Owner"},
    "person": {"name": "Name", "emails": "Emails", "linkedinLink": "Linkedin", "jobTitle": "Job Title",
               "phones": "Phones", "company": "Company"},
    "opportunity": {"name": "Name", "amount": "Amount", "closeDate": "Close date", "stage": "Stage",
                    "pointOfContact": "Point of Contact", "company": "Company", "owner": "Owner"},
    "task": {"title": "Title", "bodyV2": "Body", "dueAt": "Due Date", "status": "Status", "assignee": "Assignee"},
    "note": {"title": "Title", "bodyV2": "Body"},
    "workspaceMember": {"name": "Name", "userEmail": "User Email"},
    "attachment": {"name": "Name", "file": "File"},
}
STANDARD_SYSTEM_LABELS = {
    "id": "Id", "crm_created_at": "Creation date", "crm_updated_at": "Last update", "crm_deleted_at": "Deleted at",
}
SYSTEM_IGNORED = {
    "updatedAt", "Last update", "deletedAt", "Deleted at", "searchVector", "createdBy",
    "updatedBy", "Created by", "Updated by", "crm_updated_at", "crm_deleted_at", "crm_title", "crm_object",
    "crm_created_by", "crm_updated_by", "crm_created_source", "crm_position", "__typename",
}


def standard_field_label(object_name: str, field_name: str, definition: dict[str, Any]) -> str:
    """The export column label: the standard label for standard fields, otherwise the data model label."""
    standard = STANDARD_FIELD_LABELS.get(object_name, {}).get(field_name)
    if standard:
        return standard
    if definition.get("standard"):
        return humanize(field_name)
    return str(definition.get("label") or humanize(field_name))


def standard_column(object_name: str, field_name: str, definition: dict[str, Any], sub: str = "") -> str:
    """Header of one export column: 'Label', 'Label / Sublabel', 'Label Id'."""
    label = standard_field_label(object_name, field_name, definition)
    ftype = definition.get("type")
    if ftype == "RELATION":
        return f"{label} Id"
    if sub:
        sub_label = STANDARD_SUBFIELD_LABELS.get(ftype, {}).get(sub, humanize(sub))
        return f"{label} / {sub_label}"
    return label
