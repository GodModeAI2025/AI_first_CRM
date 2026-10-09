#!/usr/bin/env python3
"""Generate the read-only CRM pages under graph/crm/ from records, events, views and dashboards.

graph/crm/index.html                  start page: objects, views, dashboards, workflows, recent activity
graph/crm/objects/<object-dir>.html   record browser: search, sorting, trash, paging, details via #<uuid>
graph/crm/views/<view-id>.html        table, kanban or calendar view from schema/crm/views.json
graph/crm/dashboards/<id>.html        dashboard from schema/crm/dashboards.json, charts as inline SVG
graph/crm/manifest.json               lmwiki-crm-build/1: inputs_sha256, generated_at, files {path: sha256}

Every page embeds its data as JSON, loads nothing from the network, links only
relatively and works from the file system without a server. The output is
byte-identical for identical inputs and --now (default: the current time).
Files are staged outside the wiki first, then replaced one by one; files that
are no longer produced are removed, and only below graph/crm/. The manifest is
written last, so an interrupted build is reported as stale by check_fresh.

Usage:
  crm_build.py --target <wiki> --lock-token <token> [--now 2026-10-06T15:00:00Z]
  crm_build.py --target <wiki> --lock-token <token> --check    (freshness only, writes nothing)
Exit codes: 0 built or fresh, 2 error, 3 stale (--check), 4 invalid view or dashboard definitions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import hashlib
import html
import json
import posixpath
import re
import tempfile
import time
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Optional
from urllib.parse import quote

import crm_svg
import crm_views as V
import portable_io
from crm_contract import (
    DASHBOARDS_PATH, DATAMODEL_PATH, EVENTS_DIR, FILES_DIR, OUTBOX_DIR, RECORDS_DIR, ROLES_PATH, SETTINGS_PATH, VIEWS_PATH,
    WORKFLOWS_DIR, CrmError, DataModel, Record, artifact_files, files_items, format_instant, kebab, load_datamodel,
    parse_instant, parse_record_link, utc_now,
)
from crm_filters import FilterError
from wiki_lock import require_lock

BUILD_FORMAT = "lmwiki-crm-build/1"
RENDERER = "crm_build/2"
OUTPUT_DIR = "graph/crm"
MANIFEST_PATH = f"{OUTPUT_DIR}/manifest.json"
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "crm-template.html"
SYNC_NOISE = {".DS_Store", "Thumbs.db", "desktop.ini"}
WIKI_RECORD_LINK = re.compile(
    r"\[\[(records/[a-z0-9]+(?:-[a-z0-9]+)*/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"
)
PLACEHOLDER = re.compile(r"__([A-Z_]+)__")
KANBAN_VISIBLE_CARDS = 50
RECENT_EVENTS = 50
ACTIVITY_OBJECTS = ("message", "calendarEvent")
OPS = ["create", "update", "upsert", "delete", "restore", "destroy", "merge", "erase", "cascade-null", "erased"]

UI: dict[str, dict[str, Any]] = {
    "de": {
        "brand": "AI First CRM · CRM", "skip": "Zum Inhalt springen", "nav": "CRM-Navigation",
        "home": "CRM-Übersicht", "graph": "Wissensgraph", "crm": "CRM", "objects": "Objekte", "views": "Ansichten",
        "dashboards": "Dashboards", "workflows": "Workflows", "activity": "Letzte Aktivitäten",
        "outbox": "Postausgang", "outbox_note": "Entwürfe aus Workflows und Kampagnen. Nichts davon wurde versendet; zum Prüfen und Senden die Datei im Mailprogramm öffnen.",
        "stored": "Dateiablage: {count} Dateien, {size}.", "more_files": "und {count} weitere",
        "themes": {"system": "System", "light": "Hell", "dark": "Dunkel"}, "themeAria": "Farbschema:",
        "stand": "Stand: <strong>{when}</strong> ({zone}).",
        "stand_note": "Relative Filter wie „diesen Monat“ oder „heute“ wurden zu diesem Zeitpunkt ausgewertet. "
                      "Spätere Änderungen erscheinen erst nach dem nächsten Erzeugen.",
        "footer": "Diese Seite wird aus den Datensätzen unter records/ und dem Ereignisprotokoll erzeugt und ist nur lesbar. "
                  "Änderungen laufen über Transaktionen; danach werden die Seiten neu erzeugt.",
        "noscript": "Diese Ansicht braucht JavaScript. Die Rohdaten liegen unter records/.",
        "records_count": "{count} Datensätze", "records_count_one": "1 Datensatz", "trash_count": "{count} im Papierkorb",
        "inactive": "deaktiviert",
        "type_table": "Tabelle", "type_kanban": "Kanban", "type_calendar": "Kalender",
        "active": "aktiv", "not_active": "inaktiv", "unknown": "Status unbekannt", "unreadable": "Datei nicht lesbar",
        "none": "Keine Einträge.", "time": "Zeit", "actor": "Akteur", "action": "Aktion", "object": "Objekt",
        "record": "Datensatz", "gone": "nicht mehr vorhanden", "erased_record": "gelöschter Datensatz",
        "restricted": "Eingeschränkt auf Rollen: {roles}. Die Rollen sind kooperativ; die Datei selbst schützt nichts.",
        "search": "Suchen", "search_in": "In {label} suchen", "trash": "Papierkorb anzeigen",
        "prev": "Zurück", "next": "Weiter", "range": "{from} bis {to} von {total}", "noRows": "Keine Datensätze.",
        "back": "Zur Liste", "fields": "Felder", "empty": "leer", "related": "Verknüpfte Datensätze",
        "via_people": "{label} der Personen", "via_company": "{label} zur Verkaufschance", "no_value": "kein Wert",
        "relGroup": "{label} ({count})", "showAll": "Alle {count} anzeigen", "timeline": "Verlauf",
        "changed": "Geänderte Felder: {fields}", "wiki": "Wiki-Seiten mit Verweis", "noWiki": "Keine Wiki-Seite verweist auf diesen Datensatz.",
        "system": "Systemfelder", "raw": "Markdown-Rohdatei öffnen", "idLabel": "ID", "inTrash": "Im Papierkorb seit {when}",
        "notFound": "Datensatz {id} wurde nicht gefunden. Er wurde vielleicht endgültig entfernt oder gehört zu einem anderen Objekt.",
        "toggle": "Gruppe {label} ein- oder ausklappen", "groupTotal": "Gruppe", "total": "Gesamt",
        "today": "Heute", "month": "Monat", "week": "Woche", "more": "{count} weitere", "weekRange": "{from} bis {to}",
        "lead_object": "{records}. {trash}.", "lead_view": "{type} über {object}: {records}.",
        "filter": "Filter: {text}", "sort": "Sortierung: {text}", "asc": "aufsteigend", "desc": "absteigend",
        "group_by": "Gruppiert nach {field}.",
        "undated": "Ohne Datum ({count})", "cards": "{count} Karten", "cards_one": "1 Karte", "more_cards": "Weitere {count} anzeigen",
        "since": "seit {when} in dieser Phase", "expected": "Erwartet", "probability": "{value} Wahrscheinlichkeit",
        "board_total": "Gesamt: {records}", "no_data": "Keine Daten.",
        "hidden": "{count} weitere Gruppen nicht angezeigt (nicht angezeigte Daten). Filter eingrenzen oder eine gröbere Datumsstufe wählen.",
        "hidden_one": "1 weitere Gruppe wird nicht angezeigt (nicht angezeigte Daten). Filter eingrenzen oder eine gröbere Datumsstufe wählen.",
        "hidden_rows": "{count} weitere Zeilen in der Ansicht.", "hidden_rows_one": "1 weitere Zeile in der Ansicht.",
        "folded": "{count} kleinere Gruppen sind als „{other}“ zusammengefasst.",
        "folded_one": "1 kleinere Gruppe ist als „{other}“ zusammengefasst.", "cumulative": "Kumulierte Werte.",
        "data_table": "Daten als Tabelle", "category": "Kategorie", "value": "Wert", "single": "Einzelwert",
        "open_view": "Ansicht öffnen", "currency_panel": "Währung {currency}",
        "link_note": "Externe Seite. Eingebettete Inhalte (iFrame) erscheinen hier als Link.",
        "link_open": "{url} öffnen", "per_currency": "Beträge in verschiedenen Währungen werden getrennt ausgewiesen.",
        "dashboard_lead": "{count} Widgets.", "dashboard_lead_one": "1 Widget.",
        "chart_desc": "{kind} über {object}: {function} nach {group}.", "kind_bar": "Balkendiagramm",
        "kind_line": "Liniendiagramm", "kind_pie": "Kreisdiagramm",
        "functions": {"COUNT": "Anzahl", "COUNT_UNIQUE_VALUES": "Eindeutige Werte", "COUNT_EMPTY": "Leer",
                      "COUNT_NOT_EMPTY": "Nicht leer", "COUNT_TRUE": "Wahr", "COUNT_FALSE": "Falsch",
                      "PERCENTAGE_EMPTY": "Prozent leer", "PERCENTAGE_NOT_EMPTY": "Prozent nicht leer",
                      "SUM": "Summe", "AVG": "Durchschnitt", "MIN": "Minimum", "MAX": "Maximum"},
        "ops": {"create": "angelegt", "update": "geändert", "upsert": "abgeglichen", "delete": "in den Papierkorb verschoben",
                "restore": "wiederhergestellt", "destroy": "endgültig entfernt", "merge": "zusammengeführt",
                "erase": "gelöscht", "cascade-null": "Verknüpfung entfernt", "erased": "gelöscht"},
        "origins": {"manual": "manuell", "import": "Import", "email": "E-Mail", "calendar": "Kalender",
                    "workflow": "Workflow", "merge": "Zusammenführung", "restore": "Wiederherstellung", "agent": "Agent",
                    "api": "API", "migration": "Migration"},
        "row": "Zeile {row}", "zone_warning": "Achtung: {problem}",
    },
    "en": {
        "brand": "AI First CRM · CRM", "skip": "Skip to content", "nav": "CRM navigation",
        "home": "CRM overview", "graph": "Knowledge graph", "crm": "CRM", "objects": "Objects", "views": "Views",
        "dashboards": "Dashboards", "workflows": "Workflows", "activity": "Recent activity",
        "outbox": "Outbox", "outbox_note": "Drafts from workflows and campaigns. None of them was sent; open a file in the mail program to check and send it.",
        "stored": "File store: {count} files, {size}.", "more_files": "and {count} more",
        "themes": {"system": "System", "light": "Light", "dark": "Dark"}, "themeAria": "Colour scheme:",
        "stand": "As of <strong>{when}</strong> ({zone}).",
        "stand_note": "Relative filters such as “this month” or “today” were evaluated at this moment. "
                      "Later changes appear after the next build.",
        "footer": "This page is generated from the records under records/ and the event log and is read-only. "
                  "Changes go through transactions; the pages are rebuilt afterwards.",
        "noscript": "This view needs JavaScript. The raw data lives under records/.",
        "records_count": "{count} records", "records_count_one": "1 record", "trash_count": "{count} in trash",
        "inactive": "deactivated",
        "type_table": "Table", "type_kanban": "Kanban", "type_calendar": "Calendar",
        "active": "active", "not_active": "inactive", "unknown": "status unknown", "unreadable": "file not readable",
        "none": "No entries.", "time": "Time", "actor": "Actor", "action": "Action", "object": "Object",
        "record": "Record", "gone": "no longer present", "erased_record": "erased record",
        "restricted": "Restricted to roles: {roles}. Roles are cooperative; the file itself protects nothing.",
        "search": "Search", "search_in": "Search {label}", "trash": "Show trash",
        "prev": "Previous", "next": "Next", "range": "{from} to {to} of {total}", "noRows": "No records.",
        "back": "Back to list", "fields": "Fields", "empty": "empty", "related": "Related records",
        "via_people": "{label} of its people", "via_company": "{label} of this opportunity", "no_value": "no value",
        "relGroup": "{label} ({count})", "showAll": "Show all {count}", "timeline": "Timeline",
        "changed": "Changed fields: {fields}", "wiki": "Wiki pages linking here", "noWiki": "No wiki page links to this record.",
        "system": "System fields", "raw": "Open raw Markdown file", "idLabel": "ID", "inTrash": "In trash since {when}",
        "notFound": "Record {id} was not found. It may have been removed permanently or belong to another object.",
        "toggle": "Collapse or expand group {label}", "groupTotal": "Group", "total": "Total",
        "today": "Today", "month": "Month", "week": "Week", "more": "{count} more", "weekRange": "{from} to {to}",
        "lead_object": "{records}. {trash}.", "lead_view": "{type} of {object}: {records}.",
        "filter": "Filter: {text}", "sort": "Sorted by {text}", "asc": "ascending", "desc": "descending",
        "group_by": "Grouped by {field}.",
        "undated": "Without date ({count})", "cards": "{count} cards", "cards_one": "1 card", "more_cards": "Show {count} more",
        "since": "in this stage since {when}", "expected": "Expected", "probability": "{value} probability",
        "board_total": "Total: {records}", "no_data": "No data.",
        "hidden": "{count} more groups are not displayed (undisplayed data). Narrow the filter or choose a coarser date granularity.",
        "hidden_one": "1 more group is not displayed (undisplayed data). Narrow the filter or choose a coarser date granularity.",
        "hidden_rows": "{count} more rows in the view.", "hidden_rows_one": "1 more row in the view.",
        "folded": "{count} smaller groups are combined as “{other}”.",
        "folded_one": "1 smaller group is shown as “{other}”.", "cumulative": "Cumulative values.",
        "data_table": "Data as table", "category": "Category", "value": "Value", "single": "Single value",
        "open_view": "Open view", "currency_panel": "Currency {currency}",
        "link_note": "External page. Embedded content (iFrame) appears here as a link.",
        "link_open": "Open {url}", "per_currency": "Amounts in different currencies are shown separately.",
        "dashboard_lead": "{count} widgets.", "dashboard_lead_one": "1 widget.",
        "chart_desc": "{kind} of {object}: {function} by {group}.", "kind_bar": "Bar chart",
        "kind_line": "Line chart", "kind_pie": "Pie chart",
        "functions": {"COUNT": "Count", "COUNT_UNIQUE_VALUES": "Unique values", "COUNT_EMPTY": "Empty",
                      "COUNT_NOT_EMPTY": "Not empty", "COUNT_TRUE": "True", "COUNT_FALSE": "False",
                      "PERCENTAGE_EMPTY": "Percent empty", "PERCENTAGE_NOT_EMPTY": "Percent not empty",
                      "SUM": "Sum", "AVG": "Average", "MIN": "Min", "MAX": "Max"},
        "ops": {"create": "created", "update": "updated", "upsert": "upserted", "delete": "moved to trash",
                "restore": "restored", "destroy": "removed permanently", "merge": "merged",
                "erase": "erased", "cascade-null": "link removed", "erased": "erased"},
        "origins": {"manual": "manual", "import": "import", "email": "e-mail", "calendar": "calendar",
                    "workflow": "workflow", "merge": "merge", "restore": "restore", "agent": "agent",
                    "api": "API", "migration": "migration"},
        "row": "row {row}", "zone_warning": "Warning: {problem}",
    },
}


class BuildInvalid(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


# ---------------------------------------------------------------------------
# inputs and freshness


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_paths(target: Path) -> list[str]:
    """Every file whose content the generated pages depend on, wiki-relative and sorted."""
    paths = [relative for relative in (DATAMODEL_PATH, SETTINGS_PATH, VIEWS_PATH, DASHBOARDS_PATH, ROLES_PATH)
             if (target / relative).is_file()]
    workflows = target / WORKFLOWS_DIR
    if workflows.is_dir():
        paths.extend(path.relative_to(target).as_posix() for path in workflows.glob("*.json") if path.is_file())
    records = target / RECORDS_DIR
    if records.is_dir():
        paths.extend(path.relative_to(target).as_posix() for path in records.glob("*/*.md") if path.is_file())
    events = target / EVENTS_DIR
    if events.is_dir():
        paths.extend(path.relative_to(target).as_posix() for path in events.glob("*.jsonl") if path.is_file())
    return sorted(paths)


def wiki_backlinks(target: Path) -> list[dict[str, Any]]:
    """Wiki pages under wiki/ that link to records: [{path, title, records}] sorted by path."""
    from frontmatter_contract import FrontmatterError, parse_document

    pages = []
    root = target / "wiki"
    if not root.is_dir():
        return pages
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        ids = sorted({match.group(2) for match in WIKI_RECORD_LINK.finditer(text)})
        if not ids:
            continue
        title = ""
        try:
            document = parse_document(text, path.name)
            title = str(document.data.get("title") or "")
            body = document.body
        except FrontmatterError:
            body = text
        if not title:
            heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            title = heading.group(1).strip() if heading else path.stem
        pages.append({"path": path.relative_to(target).as_posix(), "title": title, "records": ids})
    return pages


def expected_inputs_sha256(target: Path) -> str:
    """Hash of everything graph/crm depends on: the CRM definitions it reads (data model, settings,
    views, dashboards, roles, workflows), the wiki language, records, events, the wiki pages that
    link to records (path, title and targets only) and the page template. The generator code is
    represented by RENDERER, which changes whenever the output format changes."""
    target = Path(target).expanduser().resolve()
    lines = [f"renderer\0{RENDERER}", f"language\0{V.ui_language(V.wiki_language(target))}"]
    if TEMPLATE_PATH.is_file():
        lines.append(f"template\0{_sha256_file(TEMPLATE_PATH)}")
    for relative in input_paths(target):
        lines.append(f"{relative}\0{_sha256_file(target / relative)}")
    backlinks = json.dumps(wiki_backlinks(target), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    lines.append(f"wiki-backlinks\0{hashlib.sha256(backlinks.encode('utf-8')).hexdigest()}")
    # Stored files are named by their hash and drafts are never rewritten, so names and sizes suffice.
    listing = json.dumps([[relative, (target / relative).stat().st_size] for relative in artifact_files(target)], separators=(",", ":"))
    lines.append(f"artifacts\0{hashlib.sha256(listing.encode('utf-8')).hexdigest()}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _output_files(target: Path) -> set[str]:
    root = target / OUTPUT_DIR
    if not root.is_dir():
        return set()
    return {path.relative_to(target).as_posix() for path in root.rglob("*") if path.is_file()}


def check_fresh(target: Path) -> list[str]:
    """Problems that make graph/crm unfit for a release; empty when it matches its inputs. Read-only, no lock."""
    target = Path(target).expanduser().resolve()
    if not (target / DATAMODEL_PATH).is_file():
        if (target / OUTPUT_DIR).exists():
            return [f"{OUTPUT_DIR}/ exists but the wiki has no CRM layer ({DATAMODEL_PATH} is missing)"]
        return []
    manifest_file = target / MANIFEST_PATH
    if not manifest_file.is_file():
        return [f"{OUTPUT_DIR}/ is missing or incomplete; build the CRM pages with crm_build.py"]
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"{MANIFEST_PATH} is unreadable ({exc}); build the CRM pages again"]
    if not isinstance(manifest, dict) or manifest.get("format") != BUILD_FORMAT or not isinstance(manifest.get("files"), dict):
        return [f"{MANIFEST_PATH} is not a {BUILD_FORMAT} manifest; build the CRM pages again"]
    errors = []
    if manifest.get("inputs_sha256") != expected_inputs_sha256(target):
        errors.append(
            f"{OUTPUT_DIR}/ is stale: records, events, CRM definitions, wiki links to records or the page template "
            "changed after the last build; run crm_build.py"
        )
    files = manifest["files"]
    for relative, digest in sorted(files.items()):
        path = target / relative
        if not str(relative).startswith(OUTPUT_DIR + "/") or not path.is_file():
            errors.append(f"{relative} is listed in {MANIFEST_PATH} but missing")
        elif _sha256_file(path) != digest:
            errors.append(f"{relative} differs from {MANIFEST_PATH}; generated pages must not be edited by hand")
    extra = sorted(path for path in _output_files(target) - set(files) - {MANIFEST_PATH}
                   if path.rsplit("/", 1)[-1] not in SYNC_NOISE)
    if extra:
        errors.append(f"files under {OUTPUT_DIR}/ are not in its manifest: {extra[:10]}")
    return errors


# ---------------------------------------------------------------------------
# HTML helpers


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def embed_json(value: Any) -> str:
    """JSON that is safe inside <script type="application/json">: no '<', '>' or '&' survives literally."""
    def refuse(item: Any) -> Any:
        raise TypeError(f"not serializable in page data: {type(item).__name__}")

    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=refuse)
    return (text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


OUTBOX_SHOWN = 200


def human_size(size: int, language: str) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            text = f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            return text.replace(".", ",") if str(language).startswith("de") else text
        value /= 1024
    return f"{size} B"


def href_path(from_dir: str, to_path: str) -> str:
    """Relative, percent-encoded link between two wiki-relative paths."""
    relative = posixpath.relpath(to_path, start=from_dir)
    return "/".join(quote(part, safe="") for part in relative.split("/"))


def safe_url(value: Any, *, bare_domains: bool = False) -> Optional[str]:
    """http(s) and mailto links only; a bare domain becomes https when allowed."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"(?i)https?://[^\s<>\"'`]+", text) or re.fullmatch(r"(?i)mailto:[^\s<>\"'`]+", text):
        return text
    if bare_domains and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?:[/?#][^\s<>\"'`]*)?", text):
        return "https://" + text
    return None


def external_link(url: str, label: str) -> str:
    return f'<a href="{esc(url)}" rel="noopener noreferrer" target="_blank">{esc(label)}</a>'


def _emphasis(escaped: str) -> str:
    escaped = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<![*\w])\*(?=\S)(.+?)(?<=\S)\*(?![*\w])", r"<em>\1</em>", escaped)
    return re.sub(r"(?<![_\w])_(?=\S)(.+?)(?<=\S)_(?![_\w])", r"<em>\1</em>", escaped)


def _inline(text: str, wikilink: Optional[Callable[[str], Optional[str]]]) -> str:
    out = []
    for part in re.split(r"(`[^`]+`)", text):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{esc(part[1:-1])}</code>")
            continue
        for token in re.split(r"(\[\[[^\]]+\]\]|\[[^\]\n]+\]\([^)\s]+\))", part):
            wiki = re.fullmatch(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]", token)
            markdown = re.fullmatch(r"\[([^\]\n]+)\]\(([^)\s]+)\)", token)
            if wiki:
                target, label = wiki.group(1).strip(), (wiki.group(2) or wiki.group(1).rsplit("/", 1)[-1]).strip()
                link = wikilink(target) if wikilink else None
                out.append(f'<a href="{esc(link)}">{_emphasis(esc(label))}</a>' if link else f"<span>{_emphasis(esc(label))}</span>")
            elif markdown:
                url = safe_url(markdown.group(2))
                label = _emphasis(esc(markdown.group(1)))
                out.append(f'<a href="{esc(url)}" rel="noopener noreferrer" target="_blank">{label}</a>' if url else esc(token))
            else:
                out.append(_emphasis(esc(token)))
    return "".join(out)


def markdown_html(text: str, wikilink: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """A safe Markdown subset: everything is escaped first; links only http(s), mailto and wiki targets."""
    out: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    list_kind = ""
    quote_lines: list[str] = []
    code: list[str] = []
    in_code = False

    def flush() -> None:
        nonlocal paragraph, items, list_kind, quote_lines
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(line, wikilink) for line in paragraph) + "</p>")
            paragraph = []
        if items:
            out.append(f"<{list_kind}>" + "".join(f"<li>{_inline(item, wikilink)}</li>" for item in items) + f"</{list_kind}>")
            items, list_kind = [], ""
        if quote_lines:
            out.append("<blockquote>" + "<br>".join(_inline(line, wikilink) for line in quote_lines) + "</blockquote>")
            quote_lines = []

    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
                code, in_code = [], False
            else:
                flush()
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        if not stripped:
            flush()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        quoted = re.match(r"^>\s?(.*)$", stripped)
        if heading:
            flush()
            level = min(6, len(heading.group(1)) + 2)
            out.append(f"<h{level}>{_inline(heading.group(2), wikilink)}</h{level}>")
        elif bullet or ordered:
            kind = "ul" if bullet else "ol"
            if paragraph or quote_lines or (items and list_kind != kind):
                flush()
            list_kind = kind
            items.append((bullet or ordered).group(1))
        elif quoted:
            if paragraph or items:
                flush()
            quote_lines.append(quoted.group(1))
        else:
            if items or quote_lines:
                flush()
            paragraph.append(stripped)
    if in_code:
        out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
    flush()
    return "".join(out)


# ---------------------------------------------------------------------------
# build context


class Build:
    """Everything one build needs: data, definitions, texts, and link helpers."""

    def __init__(self, target: Path, now: str):
        self.target = target
        self.dm: DataModel = load_datamodel(target)
        self.language = V.wiki_language(target)
        self.lang = V.ui_language(self.language)
        self.ui = UI[self.lang]
        self.settings = V.load_settings(target, self.language)
        self.views_doc = V.load_views(target)
        self.dashboards_doc = V.load_dashboards(target)
        self.roles = V.load_roles(target)
        errors = V.validate_views(self.dm, self.views_doc, roles=self.roles)
        errors += V.validate_dashboards(self.dm, self.dashboards_doc, self.views_doc, roles=self.roles)
        if errors:
            raise BuildInvalid(errors)
        self.views = V.normalize_views(self.dm, self.views_doc)
        self.dashboards = V.normalize_dashboards(self.dm, self.dashboards_doc)
        self.ds = V.Dataset(target, self.dm, settings=self.settings, language=self.language, now=now)
        self.now = now
        self.warnings: list[str] = []
        problem = V.zone_problem(self.settings, self.ds.context)
        if problem:
            self.warnings.append(problem)
        self.zone_problem = problem
        self.backlinks: dict[str, list[tuple[str, str]]] = {}
        for page in wiki_backlinks(target):
            for record_id in page["records"]:
                self.backlinks.setdefault(record_id, []).append((page["path"], page["title"]))
        self.template = TEMPLATE_PATH.read_text(encoding="utf-8")
        for problem in self.ds.store.load_errors:
            self.warnings.append(f"unreadable record skipped: {problem}")
        bound = [(f"view {view['id']}", view.get("me")) for view in self.views]
        bound += [(f"widget {dashboard['id']}/{widget['id']}", widget.get("me")) for dashboard in self.dashboards for widget in dashboard["widgets"]]
        for where, member in bound:
            if member and self.ds.titles.get(member, ("",))[0] != "workspaceMember":
                self.warnings.append(f"{where}: me {member} is not an existing workspaceMember; @me matches nothing")

    # -- links --------------------------------------------------------------

    def object_page(self, object_name: str) -> str:
        return f"{OUTPUT_DIR}/objects/{kebab(object_name)}.html"

    def record_href(self, from_dir: str, record_id: str) -> Optional[str]:
        found = self.ds.titles.get(record_id)
        if not found:
            return None
        return href_path(from_dir, self.object_page(found[0])) + "#" + record_id

    def wikilink(self, from_dir: str) -> Callable[[str], Optional[str]]:
        def resolve(target: str) -> Optional[str]:
            target = target.strip().removesuffix(".md")
            parsed = parse_record_link(f"[[{target}]]")
            if parsed:
                return self.record_href(from_dir, parsed[1])
            if target.startswith(("wiki/", "sources/")) and (self.target / f"{target}.md").is_file():
                return href_path(from_dir, f"graph/pages/{target}.html")
            return None
        return resolve

    # -- page frame ---------------------------------------------------------

    def stand_html(self) -> str:
        zone = str(self.settings.get("time_zone") or "UTC")
        text = self.ui["stand"].format(when=esc(self.ds.format_instant(self.now)), zone=esc(zone))
        extra = f' <span class="warn">{esc(self.ui["zone_warning"].format(problem=self.zone_problem))}</span>' if self.zone_problem else ""
        return f'<p class="stand">{text} {esc(self.ui["stand_note"])}{extra}</p>'

    def page(self, path: str, *, kind: str, title: str, crumbs: list[tuple[str, Optional[str]]], body: str,
             data: dict[str, Any]) -> bytes:
        here = posixpath.dirname(path)
        crumb_html = " › ".join(
            f'<a href="{esc(href_path(here, target))}">{esc(label)}</a>' if target else esc(label)
            for label, target in crumbs
        )
        nav = (f'<a class="button" href="{esc(href_path(here, OUTPUT_DIR + "/index.html"))}">{esc(self.ui["home"])}</a>'
               f'<a class="button" href="{esc(href_path(here, "graph/index.html"))}">{esc(self.ui["graph"])}</a>')
        payload = {"page": kind, "lang": self.lang}
        payload.update(data)
        payload["t"] = {"themes": self.ui["themes"], "themeAria": self.ui["themeAria"], **payload.get("t", {})}
        values = {
            "LANG": self.lang, "TITLE": esc(f"{title} · {self.ui['brand']}"), "PAGE": esc(kind), "SKIP": esc(self.ui["skip"]),
            "BRAND": esc(self.ui["brand"]), "CRUMBS": crumb_html, "NAV_LABEL": esc(self.ui["nav"]), "NAV": nav,
            "STAND": self.stand_html(), "BODY": body, "FOOTER": esc(self.ui["footer"]), "DATA": embed_json(payload),
        }
        return PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), self.template).encode("utf-8")

    def js_texts(self, *keys: str) -> dict[str, Any]:
        return {key: self.ui[key] for key in keys}

    def counted(self, key: str, count: int, **values: Any) -> str:
        """A UI text with a count; the singular variant is used for exactly one."""
        template = self.ui.get(f"{key}_one") if count == 1 else None
        return (template or self.ui[key]).format(count=self.ds.format_number(Decimal(count)), **values)


# ---------------------------------------------------------------------------
# compact cells shared by object pages and table views


class Cells:
    """Encodes field values as compact cells; relation targets go into a per-page table."""

    def __init__(self, build: Build, page_dir: str):
        self.b = build
        self.ds = build.ds
        self.page_dir = page_dir
        self.refs: list[list[Any]] = []
        self.ref_index: dict[str, int] = {}
        self.dirs: list[list[str]] = []
        self.dir_index: dict[str, int] = {}
        self.actors: list[str] = []
        self.actor_index: dict[str, int] = {}

    def ref(self, record_id: str) -> Optional[int]:
        if record_id in self.ref_index:
            return self.ref_index[record_id]
        found = self.ds.titles.get(record_id)
        if not found:
            return None
        object_name, title = found
        if object_name not in self.dir_index:
            self.dir_index[object_name] = len(self.dirs)
            self.dirs.append([href_path(self.page_dir, self.b.object_page(object_name)), V.object_label(self.b.dm, object_name, False)])
        self.ref_index[record_id] = len(self.refs)
        self.refs.append([self.dir_index[object_name], record_id, title])
        return self.ref_index[record_id]

    def actor(self, actor: Any) -> int:
        text = str(actor or "")
        if text not in self.actor_index:
            self.actor_index[text] = len(self.actors)
            self.actors.append(text)
        return self.actor_index[text]

    def code(self, ref: V.FieldRef, label_field: str) -> str:
        if ref.key in (label_field, "crm_title"):
            return "k"
        if ref.system:
            return {"DATE_TIME": "d", "NUMBER": "n"}.get(ref.ftype, "a" if ref.key in ("crm_created_by", "crm_updated_by") else "t")
        if ref.inverse or ref.relation:
            return "r"
        if ref.sub:
            return "n" if ref.ftype == "CURRENCY" and ref.sub in ("amount", "amountMicros") else "t"
        return {"NUMBER": "n", "NUMERIC": "n", "CURRENCY": "n", "DATE": "d", "DATE_TIME": "d", "SELECT": "o",
                "MULTI_SELECT": "o", "RATING": "o", "BOOLEAN": "o", "LINKS": "u", "EMAILS": "u", "FILES": "u",
                "RICH_TEXT": "h"}.get(ref.ftype, "t")

    def cell(self, record: Record, ref: V.FieldRef, code: str, *, rich: bool = True) -> Any:
        data = record.data
        if code == "k":
            return str(data.get("crm_title") or record.id[:8])
        if code == "a":
            value = data.get(ref.key)
            return self.actor(value) if value else ""
        if ref.key == "crm_created_source":
            value = str(data.get(ref.key) or "")
            return self.b.ui["origins"].get(value.lower(), value)
        if code == "r":
            if ref.inverse:
                relation = ref.definition["relation"]
                ids = [source_id for source_object, field_name, source_id in self.ds.incoming.get(record.id, [])
                       if source_object == relation.get("target") and field_name == relation.get("inverse")]
                ids.sort(key=lambda value: (self.ds.title(value).casefold(), value))
            else:
                ids = V.crm_filters.raw_value(self.b.dm, record, ref.key)
            return [index for index in (self.ref(value) for value in ids) if index is not None]
        if code == "u":
            return self._links(record, ref)
        if code == "h":
            text = record.richtext.get(ref.base, "")
            if not text.strip():
                return ""
            return markdown_html(text, self.b.wikilink(self.page_dir)) if rich else self.ds.display(record, ref.key)
        display = self.ds.display(record, ref.key)
        if display == "":
            return ""
        if code == "n":
            if ref.ftype == "CURRENCY":
                micros = data.get(f"{ref.base}.amountMicros")
                return [int(micros) if micros is not None else 0, display]
            number = V.to_decimal(data.get(ref.key if ref.system else ref.base))
            return [float(number) if number is not None else 0, display]
        if code == "d":
            raw = data.get(ref.key if ref.system else ref.base)
            moment = parse_instant(raw) if isinstance(raw, str) and "T" in raw else None
            if moment is not None:
                return [int(moment.timestamp() // 60), display]
            try:
                day = date.fromisoformat(str(raw)[:10])
                return [day.toordinal() * 1440, display]
            except ValueError:
                return display
        if code == "o":
            raw = data.get(ref.base)
            if ref.ftype == "BOOLEAN":
                return [0 if raw else 1, display]
            if ref.ftype == "RATING":
                return [V.RATING_VALUES.index(raw) if raw in V.RATING_VALUES else 9, display]
            options = [option.get("value") for option in ref.options]
            first = raw[0] if isinstance(raw, list) and raw else raw
            return [options.index(first) if first in options else len(options), display]
        return display

    def _links(self, record: Record, ref: V.FieldRef) -> list[list[str]]:
        data = record.data
        base = ref.base
        result: list[list[str]] = []
        if ref.ftype == "FILES":
            for item in files_items(data.get(base)):
                name = str(item.get("name") or item.get("ref") or "")
                target_ref = str(item.get("ref") or "")
                if target_ref.startswith(FILES_DIR + "/"):
                    result.append([href_path(self.page_dir, target_ref), name])
                else:
                    result.append([safe_url(target_ref) or "", name])
            return result
        if ref.ftype == "EMAILS":
            addresses = [data.get(f"{base}.primaryEmail")] + list(data.get(f"{base}.additionalEmails") or [])
            for address in addresses:
                if isinstance(address, str) and address.strip():
                    url = safe_url("mailto:" + address.strip())
                    result.append([url, address.strip()] if url else ["", address.strip()])
            return result
        primary = data.get(f"{base}.primaryLinkUrl")
        if isinstance(primary, str) and primary.strip():
            url = safe_url(primary, bare_domains=True)
            label = str(data.get(f"{base}.primaryLinkLabel") or "").strip() or re.sub(r"^(?i:https?://)", "", primary.strip()).rstrip("/")
            result.append([url or "", label])
        for item in V.json_list(data.get(f"{base}.secondaryLinks")):
            if isinstance(item, dict):
                link = item.get("url") or item.get("primaryLinkUrl")
                if isinstance(link, str) and link.strip():
                    url = safe_url(link, bare_domains=True)
                    label = str(item.get("label") or "").strip() or link.strip()
                    result.append([url or "", label])
        return result

    def tables(self) -> dict[str, Any]:
        return {"refs": self.refs, "dirs": self.dirs, "actors": self.actors}


# ---------------------------------------------------------------------------
# object pages


SYSTEM_COLUMNS = ("crm_created_at", "crm_updated_at", "crm_created_by", "crm_updated_by", "crm_created_source",
                  "crm_deleted_at", "crm_position")


def object_columns(build: Build, object_name: str) -> list[tuple[V.FieldRef, str, int, int]]:
    """(field, cell code, shown in the table, system field) for the record browser."""
    dm = build.dm
    label_field = dm.label_field(object_name)
    cells = Cells(build, "")
    columns = [(V.resolve_field(dm, object_name, label_field), "k", 1, 0)]
    for name, definition in dm.stored_fields(object_name):
        if name == label_field or definition.get("active", True) is False:
            continue
        ref = V.resolve_field(dm, object_name, name)
        code = cells.code(ref, label_field)
        columns.append((ref, code, 0 if code == "h" else 1, 0))
    for key in SYSTEM_COLUMNS:
        ref = V.resolve_field(dm, object_name, key)
        columns.append((ref, cells.code(ref, label_field), 1 if key in ("crm_created_at", "crm_updated_at") else 0, 1))
    return columns


def incoming_groups(build: Build, object_name: str) -> list[tuple[str, str, str]]:
    """(source object, field, label) for every relation that may point at this object."""
    dm = build.dm
    inverse_labels = {}
    for name, definition in dm.fields(object_name).items():
        relation = definition.get("relation") or {}
        if definition.get("type") == "RELATION" and relation.get("type") == "ONE_TO_MANY":
            inverse_labels[(relation.get("target"), relation.get("inverse"))] = str(definition.get("label") or name)
    groups = []
    for source_object, field_name, definition in dm.relation_fields_targeting(object_name):
        label = inverse_labels.get((source_object, field_name))
        if not label:
            label = f"{V.object_label(dm, source_object)} · {definition.get('label') or field_name}"
        groups.append((source_object, field_name, label))
    return groups


def activity_groups(build: Build, object_name: str) -> list[tuple[str, str]]:
    """(activity object, label) of the derived groups of the Emails and Calendar tabs of a record page.

    A company lists the mails and meetings its people took part in; an opportunity those of its
    point of contact and of its company's people. Participants only link people and members, so
    these are computed.
    """
    dm = build.dm
    if object_name not in ("company", "opportunity") or "person" not in dm.objects or "company" not in dm.fields("person"):
        return []
    if object_name == "opportunity" and "company" not in dm.fields("opportunity"):
        return []
    key = "via_people" if object_name == "company" else "via_company"
    return [(activity, build.ui[key].format(label=V.object_label(dm, activity)))
            for activity in ACTIVITY_OBJECTS if activity in dm.objects]


def company_activity(build: Build, company_id: str, cache: dict[str, dict[str, set[str]]]) -> dict[str, set[str]]:
    """Mails and meetings of the people whose company is this one, by activity object."""
    if company_id not in cache:
        found: dict[str, set[str]] = {activity: set() for activity in ACTIVITY_OBJECTS}
        for source_object, field_name, person_id in build.ds.incoming.get(company_id, []):
            if source_object == "person" and field_name == "company":
                for activity, _field, activity_id in build.ds.incoming.get(person_id, []):
                    if activity in found:
                        found[activity].add(activity_id)
        cache[company_id] = found
    return cache[company_id]


def origin_text(build: Build, origin: Any) -> str:
    if not isinstance(origin, dict):
        return ""
    parts = [build.ui["origins"].get(str(origin.get("kind")), str(origin.get("kind") or ""))]
    for key in ("ref", "workflow", "occasion"):
        if origin.get(key):
            parts.append(str(origin[key]))
    if origin.get("row") not in (None, ""):
        parts.append(build.ui["row"].format(row=origin["row"]))
    return " · ".join(part for part in parts if part)


def event_field_labels(build: Build, object_name: str, changes: Any) -> list[str]:
    labels: list[str] = []
    fields = build.dm.objects.get(object_name, {}).get("fields", {})
    for key in (changes or {}):
        base = str(key).split(":", 1)[1] if str(key).startswith("richtext:") else str(key).split(".", 1)[0]
        if base in V.SYSTEM_FIELDS:
            label = V.field_label(build.dm, object_name, base, build.language)
        elif base in fields:
            label = str(fields[base].get("label") or base)
        else:
            label = base
        if label not in labels:
            labels.append(label)
    return labels


def render_object(build: Build, object_name: str) -> bytes:
    dm, ds, ui = build.dm, build.ds, build.ui
    path = build.object_page(object_name)
    here = posixpath.dirname(path)
    cells = Cells(build, here)
    columns = object_columns(build, object_name)
    groups = incoming_groups(build, object_name)
    group_index = {(source, field): index for index, (source, field, _label) in enumerate(groups)}
    derived = activity_groups(build, object_name)
    activity_cache: dict[str, dict[str, set[str]]] = {}
    field_labels: list[str] = []
    field_label_index: dict[str, int] = {}
    origins: list[str] = []
    origin_index: dict[str, int] = {}
    pages: list[list[str]] = []
    page_index: dict[str, int] = {}
    rows = []
    records = V.default_order(ds, ds.records(object_name))
    for record in records:
        try:
            row_cells = [cells.cell(record, ref, code) for ref, code, _shown, _system in columns]
        except (ArithmeticError, TypeError, ValueError, KeyError, AttributeError) as exc:
            raise CrmError(f"{record.path}: unreadable value ({type(exc).__name__}: {exc}); "
                           "lint_wiki.py lists the invalid fields, fix the record and build again") from exc
        incoming = []
        for source_object, field_name, source_id in ds.incoming.get(record.id, []):
            index = group_index.get((source_object, field_name))
            ref_index = cells.ref(source_id)
            if index is not None and ref_index is not None:
                incoming.append((index, ds.title(source_id).casefold(), source_id, ref_index))
        if derived:
            companies = [record.id] if object_name == "company" else V.crm_filters.raw_value(dm, record, "company")
            contacts = V.crm_filters.raw_value(dm, record, "pointOfContact") if object_name == "opportunity" and "pointOfContact" in dm.fields(object_name) else []
            for offset, (activity, _label) in enumerate(derived):
                activity_ids = set()
                for company_id in companies:
                    activity_ids |= company_activity(build, company_id, activity_cache)[activity]
                for person_id in contacts:
                    activity_ids |= {source_id for source_object, _field, source_id in ds.incoming.get(person_id, []) if source_object == activity}
                for activity_id in activity_ids:
                    ref_index = cells.ref(activity_id)
                    if ref_index is not None:
                        incoming.append((len(groups) + offset, ds.title(activity_id).casefold(), activity_id, ref_index))
        incoming.sort()
        timeline = []
        for event in ds.events_for(record.id):
            erased = bool(event.get("erased"))
            op = "erased" if erased else str(event.get("op") or "")
            labels = [] if erased else event_field_labels(build, object_name, event.get("changes"))
            encoded = []
            for label in labels:
                if label not in field_label_index:
                    field_label_index[label] = len(field_labels)
                    field_labels.append(label)
                encoded.append(field_label_index[label])
            origin = origin_text(build, event.get("origin"))
            if origin not in origin_index:
                origin_index[origin] = len(origins)
                origins.append(origin)
            timeline.append([ds.format_instant(event.get("at")), cells.actor(event.get("actor")),
                             OPS.index(op) if op in OPS else op, encoded, origin_index[origin]])
        wiki_pages = []
        for page_path, page_title in build.backlinks.get(record.id, []):
            if page_path not in page_index:
                page_index[page_path] = len(pages)
                pages.append([href_path(here, "graph/pages/" + page_path[:-3] + ".html"), page_title])
            wiki_pages.append(page_index[page_path])
        deleted = ds.format_instant(record.data.get("crm_deleted_at")) if record.deleted else ""
        rows.append([record.id, deleted, row_cells, [[index, ref_index] for index, _t, _i, ref_index in incoming], timeline, wiki_pages])
    label = V.object_label(dm, object_name)
    trash = sum(1 for record in records if record.deleted)
    active = len(records) - trash
    lead = ui["lead_object"].format(records=build.counted("records_count", active), trash=build.counted("trash_count", trash))
    body = (
        f'<div id="list"><h1>{esc(label)}</h1><p class="lead">{esc(lead)}</p>'
        f'<div class="toolbar"><label class="sr-only" for="search">{esc(ui["search_in"].format(label=label))}</label>'
        f'<input id="search" type="search" placeholder="{esc(ui["search"])}" autocomplete="off">'
        f'<label class="check"><input id="trash" type="checkbox"> {esc(ui["trash"])}</label></div>'
        f'<div id="grid"></div><noscript><p class="panel">{esc(ui["noscript"])}</p></noscript></div>'
        f'<div id="detail" hidden></div>'
    )
    data = {
        "object": object_name, "label": label, "singular": V.object_label(dm, object_name, False),
        "cols": [[V.field_label(dm, object_name, ref.key, build.language), code, shown, system] for ref, code, shown, system in columns],
        "k": 0, "rows": rows, "incg": [label for _s, _f, label in groups] + [label for _a, label in derived], "fl": field_labels, "origins": origins,
        "ops": [ui["ops"][op] for op in OPS], "pages": pages,
        "raw": href_path(here, f"{RECORDS_DIR}/{kebab(object_name)}") + "/",
        "t": build.js_texts("prev", "next", "range", "noRows", "back", "fields", "empty", "related", "relGroup",
                            "showAll", "timeline", "changed", "wiki", "noWiki", "system", "raw", "idLabel", "inTrash",
                            "notFound", "none"),
    }
    data.update(cells.tables())
    crumbs = [(ui["crm"], OUTPUT_DIR + "/index.html"), (ui["objects"], None), (label, None)]
    return build.page(path, kind="object", title=label, crumbs=crumbs, body=body, data=data)


# ---------------------------------------------------------------------------
# views


def view_header(build: Build, view: dict[str, Any], count: int) -> str:
    ds, ui = build.ds, build.ui
    object_name = view["object"]
    parts = [f'<h1>{esc(view["label"])}</h1>']
    lead = ui["lead_view"].format(type=ui["type_" + view["type"]], object=V.object_label(build.dm, object_name),
                                  records=build.counted("records_count", count))
    parts.append(f'<p class="lead">{esc(lead)}</p>')
    if view.get("description"):
        parts.append(f'<p class="lead">{esc(view["description"])}</p>')
    notes = []
    description = V.describe_filter(ds, object_name, view.get("filter"), view.get("me"))
    if description:
        notes.append(ui["filter"].format(text=description))
    if view.get("sort"):
        notes.append(ui["sort"].format(text=", ".join(
            f"{V.field_label(build.dm, object_name, item['field'], build.language)} ({ui['asc'] if item['direction'] == 'asc' else ui['desc']})"
            for item in view["sort"])))
    if view.get("groupBy") and view["type"] == "table":
        notes.append(ui["group_by"].format(field=V.field_label(build.dm, object_name, view["groupBy"], build.language)))
    if view.get("visibility") == "restricted":
        notes.append(ui["restricted"].format(roles=", ".join(view.get("roles") or [])))
    if notes:
        parts.append('<p class="muted small">' + "<br>".join(esc(note) for note in notes) + "</p>")
    return "".join(parts)


def aggregate_text(build: Build, object_name: str, function: str, key: Optional[str], values: list[dict[str, Any]]) -> str:
    label = build.ui["functions"].get(function, function)
    return f"{label}: {build.ds.format_aggregates(object_name, function, key, values)}"


def render_table_view(build: Build, view: dict[str, Any], result: dict[str, Any], path: str) -> bytes:
    dm, ds, ui = build.dm, build.ds, build.ui
    here = posixpath.dirname(path)
    object_name = view["object"]
    label_field = dm.label_field(object_name)
    cells = Cells(build, here)
    refs = list(result["columns"])
    if not any(ref.key in (label_field, "crm_title") for ref in refs):
        refs.insert(0, V.resolve_field(dm, object_name, label_field))
    codes = [cells.code(ref, label_field) for ref in refs]
    codes = ["t" if code == "h" else code for code in codes]
    column_index = {ref.key: index for index, ref in enumerate(refs)}
    aggregates = view.get("aggregates") or {}

    def footer(values: dict[str, list[dict[str, Any]]]) -> list[list[Any]]:
        return [[column_index[key], aggregate_text(build, object_name, aggregates[key], key, items)]
                for key, items in values.items() if key in column_index]

    groups = []
    for group in result["groups"]:
        rows = [[record.id, 0, [cells.cell(record, ref, code, rich=False) for ref, code in zip(refs, codes)]]
                for record in group["records"]]
        groups.append({"l": group["label"], "c": swatch(group.get("color")), "n": group["count"], "a": footer(group["aggregates"]),
                       "rows": rows})
    extra = [aggregate_text(build, object_name, aggregates[key], key, items)
             for key, items in result["aggregates"].items() if key not in column_index]
    overall = footer(result["aggregates"])
    body = view_header(build, view, result["count"])
    if overall and view.get("groupBy"):
        body += '<p class="muted small">' + esc(ui["total"] + ": " + "; ".join(text for _index, text in overall)) + "</p>"
    if extra:
        body += '<p class="muted small">' + esc(ui["total"] + ": " + "; ".join(extra)) + "</p>"
    body += (f'<div class="toolbar"><label class="sr-only" for="search">{esc(ui["search_in"].format(label=view["label"]))}</label>'
             f'<input id="search" type="search" placeholder="{esc(ui["search"])}" autocomplete="off"></div>'
             f'<div id="groups"></div><noscript><p class="panel">{esc(ui["noscript"])}</p></noscript>')
    data = {
        "view": view["id"], "object": object_name, "grouped": bool(view.get("groupBy")),
        "cols": [[V.field_label(dm, object_name, ref.key, build.language), code, 1] for ref, code in zip(refs, codes)],
        "groups": groups, "oh": href_path(here, build.object_page(object_name)),
        "aggregates": {key: _values_json(items) for key, items in result["aggregates"].items()},
        "t": build.js_texts("prev", "next", "range", "noRows", "toggle", "groupTotal", "total"),
    }
    data.update(cells.tables())
    crumbs = [(ui["crm"], OUTPUT_DIR + "/index.html"), (ui["views"], None), (view["label"], None)]
    return build.page(path, kind="table", title=view["label"], crumbs=crumbs, body=body, data=data)


def _values_json(values: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [{"currency": item.get("currency"), "value": None if item.get("value") is None else str(item["value"])}
            for item in values or []]


OPTION_COLORS = {
    "red": "#e34948", "ruby": "#e34948", "crimson": "#e34948", "tomato": "#eb6834", "orange": "#eb6834",
    "amber": "#eda100", "yellow": "#eda100", "gold": "#c98500", "lime": "#84C041", "grass": "#008300",
    "green": "#008300", "jade": "#1baf7a", "mint": "#1baf7a", "turquoise": "#1baf7a", "cyan": "#1195EB",
    "sky": "#2a78d6", "blue": "#2a78d6", "iris": "#4a3aa7", "violet": "#4a3aa7", "purple": "#4a3aa7",
    "plum": "#e87ba4", "pink": "#e87ba4", "bronze": "#a3815a", "brown": "#a3815a", "gray": "#898781",
}


def swatch(color: Optional[str]) -> str:
    if not color:
        return ""
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        return color
    return OPTION_COLORS.get(color.lower(), "")


def card_html(build: Build, record: Record, fields: list[V.FieldRef], here: str, label_field: str, extra: str = "") -> str:
    cells = []
    for ref in fields:
        if ref.key in (label_field, "crm_title") or ref.ftype == "RICH_TEXT":
            continue
        value_html = field_html(build, record, ref, here)
        if value_html:
            cells.append(f"<dt>{esc(V.field_label(build.dm, record.object, ref.key, build.language))}</dt><dd>{value_html}</dd>")
    link = build.record_href(here, record.id) or "#"
    details = f"<dl>{''.join(cells)}</dl>" if cells else ""
    return f'<li class="card"><a href="{esc(link)}">{esc(record.data.get("crm_title") or record.id[:8])}</a>{details}{extra}</li>'


def field_html(build: Build, record: Record, ref: V.FieldRef, here: str) -> str:
    """Static HTML for one value: relations link to their record page, links stay external and safe."""
    if ref.relation or ref.inverse:
        if ref.inverse:
            relation = ref.definition["relation"]
            ids = [source_id for source_object, field_name, source_id in build.ds.incoming.get(record.id, [])
                   if source_object == relation.get("target") and field_name == relation.get("inverse")]
        else:
            ids = V.crm_filters.raw_value(build.dm, record, ref.key)
        links = []
        for record_id in ids:
            href = build.record_href(here, record_id)
            title = build.ds.title(record_id)
            links.append(f'<a href="{esc(href)}">{esc(title)}</a>' if href else esc(title))
        return ", ".join(links)
    if ref.ftype in ("LINKS", "EMAILS") and not ref.sub:
        cells = Cells(build, here)
        return ", ".join(external_link(url, label) if url else esc(label) for url, label in cells._links(record, ref))
    return esc(build.ds.display(record, ref.key))


def render_kanban_view(build: Build, view: dict[str, Any], result: dict[str, Any], path: str) -> bytes:
    dm, ds, ui = build.dm, build.ds, build.ui
    here = posixpath.dirname(path)
    object_name = view["object"]
    label_field = dm.label_field(object_name)
    aggregate = view.get("aggregate") or {"function": "COUNT", "field": None}
    group_ref = result["group"]
    columns_html = []
    columns_json = []
    for number, column in enumerate(result["columns"]):
        heading_id = f"column-{number + 1}"
        color = swatch(column.get("color"))
        dot = f'<span class="dot" style="background:{esc(color)}"></span>' if color else ""
        summary = [f'<p><strong>{esc(build.counted("cards", column["count"]))}</strong></p>']
        if aggregate["function"] != "COUNT":
            summary.append(f'<p>{esc(aggregate_text(build, object_name, aggregate["function"], aggregate.get("field"), column["aggregate"]))}</p>')
        if column.get("probability") is not None and column.get("expected") is not None:
            percent = ds.t["percent"].format(value=ds.format_number(column["probability"] * 100))
            expected = " · ".join(ds.format_money(item["value"], item.get("currency")) if ds.is_money(object_name, "SUM", view.get("expectedAmountField"))
                                  else ds.format_number(item["value"], 2) for item in column["expected"]) or ds.format_number(Decimal(0), 2)
            summary.append(f'<p>{esc(ui["expected"])}: <strong>{esc(expected)}</strong> ({esc(ui["probability"].format(value=percent))})</p>')
        cards = []
        for record in column["records"]:
            since = V.stage_since(ds, record, group_ref.base)
            extra = ""
            if since:
                extra = f'<p class="since">{esc(ui["since"].format(when=ds.format_instant(since)))}</p>'
            cards.append(card_html(build, record, result["cards"], here, label_field, extra))
        visible, hidden = cards[:KANBAN_VISIBLE_CARDS], cards[KANBAN_VISIBLE_CARDS:]
        more = ""
        if hidden:
            more = (f'<details><summary>{esc(ui["more_cards"].format(count=ds.format_number(Decimal(len(hidden)))))}</summary>'
                    f'<ol class="cards">{"".join(hidden)}</ol></details>')
        columns_html.append(
            f'<section class="column" aria-labelledby="{heading_id}"><header><h2 id="{heading_id}">{dot}{esc(column["label"])}</h2>'
            f'{"".join(summary)}</header><ol class="cards">{"".join(visible)}</ol>{more}</section>'
        )
        columns_json.append({
            "key": column["key"], "label": column["label"], "count": column["count"],
            "aggregate": _values_json(column["aggregate"]),
            "probability": None if column.get("probability") is None else str(column["probability"]),
            "expected": None if column.get("expected") is None else _values_json(column["expected"]),
            "ids": [record.id for record in column["records"]],
        })
    body = view_header(build, view, result["count"])
    totals = [ui["board_total"].format(records=build.counted("records_count", result["count"]))]
    if aggregate["function"] != "COUNT":
        totals.append(aggregate_text(build, object_name, aggregate["function"], aggregate.get("field"), result["aggregate"]))
    if result.get("expected") is not None:
        totals.append(f'{ui["expected"]}: ' + (" · ".join(ds.format_money(item["value"], item.get("currency")) for item in result["expected"])
                                               or ds.format_number(Decimal(0), 2)))
    if any(len(column["aggregate"]) > 1 for column in result["columns"]):
        totals.append(ui["per_currency"])
    body += '<p class="muted small">' + "<br>".join(esc(text) for text in totals) + "</p>"
    body += f'<div class="board">{"".join(columns_html)}</div>'
    data = {
        "view": view["id"], "object": object_name, "groupBy": view["groupBy"], "columns": columns_json,
        "aggregate": {"function": aggregate["function"], "field": aggregate.get("field"), "total": _values_json(result["aggregate"])},
        "expected": None if result.get("expected") is None else _values_json(result["expected"]),
    }
    crumbs = [(ui["crm"], OUTPUT_DIR + "/index.html"), (ui["views"], None), (view["label"], None)]
    return build.page(path, kind="kanban", title=view["label"], crumbs=crumbs, body=body, data=data)


def render_calendar_view(build: Build, view: dict[str, Any], result: dict[str, Any], path: str) -> bytes:
    dm, ds, ui = build.dm, build.ds, build.ui
    here = posixpath.dirname(path)
    object_name = view["object"]
    label_field = dm.label_field(object_name)
    extra_fields = [ref for ref in result["cards"] if ref.key not in (label_field, "crm_title") and ref.ftype != "RICH_TEXT"]
    entries = []
    for entry in result["entries"]:
        record = entry["record"]
        extra = " · ".join(text for text in (ds.display(record, ref.key) for ref in extra_fields) if text)
        entries.append([entry["date"], entry["time"], record.id, str(record.data.get("crm_title") or record.id[:8]), extra])
    body = view_header(build, view, result["count"])
    body += f'<div id="calendar"></div><noscript><p class="panel">{esc(ui["noscript"])}</p></noscript>'
    if result["undated"]:
        items = "".join(f'<li><a href="{esc(build.record_href(here, record.id) or "#")}">{esc(record.data.get("crm_title") or record.id[:8])}</a></li>'
                        for record in result["undated"][:100])
        body += (f'<details class="panel"><summary>{esc(ui["undated"].format(count=ds.format_number(Decimal(len(result["undated"])))))}</summary>'
                 f'<ul class="links">{items}</ul></details>')
    t = ds.t
    data = {
        "view": view["id"], "object": object_name, "mode": result["mode"], "today": result["today"],
        "ws": ds.context.week_start, "months": t["months"], "monthsShort": t["months_short"], "days": t["weekdays_short"],
        "entries": entries, "oh": href_path(here, build.object_page(object_name)), "undated": len(result["undated"]),
        "dateField": view["dateField"], "timeZone": str(build.settings.get("time_zone") or "UTC"),
        "t": build.js_texts("prev", "next", "today", "month", "week", "more", "weekRange"),
    }
    crumbs = [(ui["crm"], OUTPUT_DIR + "/index.html"), (ui["views"], None), (view["label"], None)]
    return build.page(path, kind="calendar", title=view["label"], crumbs=crumbs, body=body, data=data)


def render_view(build: Build, view: dict[str, Any]) -> bytes:
    path = f"{OUTPUT_DIR}/views/{view['id']}.html"
    result = V.compute_view(build.ds, view)
    if view["type"] == "kanban":
        return render_kanban_view(build, view, result, path)
    if view["type"] == "calendar":
        return render_calendar_view(build, view, result, path)
    return render_table_view(build, view, result, path)


# ---------------------------------------------------------------------------
# dashboards


def widget_style(widget: dict[str, Any]) -> str:
    layout = widget["layout"]
    span = max(1, min(12, int(layout.get("span") or 6)))
    column = layout.get("column")
    grid_column = f"{column + 1} / span {min(span, 12 - column)}" if isinstance(column, int) else f"span {span}"
    style = f"grid-column:{grid_column}"
    if isinstance(layout.get("row"), int):
        style += f";grid-row:{layout['row'] + 1} / span {max(1, int(layout.get('rowSpan') or 1))}"
    return style


def chart_formatters(build: Build, object_name: str, function: str, field: Optional[str], currency: Optional[str]):
    ds = build.ds

    def value_text(value: Optional[Decimal]) -> str:
        if value is None:
            return ""
        return ds.format_aggregate(object_name, function, field, {"currency": currency, "value": value})

    def tick_text(value: Decimal) -> str:
        return crm_svg.compact_number(value, build.lang, ds.format_number)

    return value_text, tick_text


def render_chart_widget(build: Build, widget: dict[str, Any], result: dict[str, Any], dashboard_id: str) -> tuple[str, dict[str, Any]]:
    ds, ui = build.ds, build.ui
    object_name = widget["object"]
    function = widget["aggregate"]["function"]
    field = widget["aggregate"].get("field") if function != "COUNT" else None
    kind = widget["type"]
    group_label = V.field_label(build.dm, object_name, widget["groupBy"], build.language)
    function_label = ui["functions"].get(function, function)
    if field:
        function_label = f"{function_label} {V.field_label(build.dm, object_name, field, build.language)}"
    parts = []
    panels_json = []
    multiple = len(result["panels"]) > 1
    integer = function.startswith("COUNT")
    for number, panel in enumerate(result["panels"]):
        chart_id = f"{dashboard_id}-{widget['id']}-{number + 1}"
        value_text, tick_text = chart_formatters(build, object_name, function, field, panel["currency"])
        categories = [category["label"] for category in panel["categories"]]
        description = ui["chart_desc"].format(kind=ui["kind_" + kind], object=V.object_label(build.dm, object_name),
                                              function=function_label, group=group_label)
        heading = ""
        if multiple:
            heading = f'<h3>{esc(ui["currency_panel"].format(currency=panel["currency"] or ds.t["no_currency"]))}</h3>'
        series = []
        for index, item in enumerate(panel["series"]):
            slot = "x" if item.get("other") else index
            series.append({"label": item["label"], "values": item["values"], "slot": slot})
        if not categories:
            parts.append(f'{heading}<p class="note">{esc(ui["no_data"])}</p>')
        elif kind == "bar":
            svg = crm_svg.bar_chart(chart_id, widget["label"], description, categories, series,
                                    horizontal=widget["orientation"] == "horizontal", stacked=widget["stacked"],
                                    value_text=value_text, tick_text=tick_text, integer=integer)
            legend = crm_svg.legend([(item["label"], item["slot"]) for item in series]) if len(series) > 1 else ""
            parts.append(heading + svg + legend)
        elif kind == "line":
            svg = crm_svg.line_chart(chart_id, widget["label"], description, categories, series, value_text=value_text,
                                     tick_text=tick_text, integer=integer)
            legend = crm_svg.legend([(item["label"], item["slot"]) for item in series], mark="line") if len(series) > 1 else ""
            parts.append(heading + svg + legend)
        else:
            values = panel["series"][0]["values"] if panel["series"] else []
            total = sum((value for value in values if value is not None and value > 0), Decimal(0))
            slices = []
            for index, (category, value) in enumerate(zip(panel["categories"], values)):
                percent = ds.t["percent"].format(value=ds.format_number(value * 100 / total, 1)) if total and value is not None else ""
                slices.append({"label": category["label"], "value": value, "slot": "x" if category.get("other") else index,
                               "percent": percent})
            center = value_text(panel["total"]) if widget["donut"] else ""
            svg = crm_svg.pie_chart(chart_id, widget["label"], description, slices, donut=widget["donut"],
                                    center_text=center, center_label=function_label if widget["donut"] else "",
                                    value_text=value_text)
            legend = crm_svg.legend([(f"{item['label']}: {value_text(item['value'])} ({item['percent']})", item["slot"]) for item in slices])
            parts.append(heading + f'<div class="pie">{svg}</div>' + legend)
        notes = []
        if panel["hidden"]:
            notes.append(build.counted("hidden", panel["hidden"]))
        if panel["folded"]:
            notes.append(build.counted("folded", panel["folded"], other=ds.t["other"]))
        if widget.get("cumulative"):
            notes.append(ui["cumulative"])
        parts.extend(f'<p class="note">{esc(note)}</p>' for note in notes)
        if categories:
            parts.append(data_table(build, panel, value_text, widget.get("cumulative")))
        panels_json.append({
            "currency": panel["currency"], "categories": categories, "hidden": panel["hidden"], "folded": panel["folded"],
            "total": None if panel["total"] is None else str(panel["total"]),
            "series": [{"label": item["label"], "values": [None if value is None else str(value) for value in item["values"]],
                        "raw": [None if value is None else str(value) for value in item.get("raw", item["values"])]}
                       for item in panel["series"]],
        })
    if multiple:
        parts.append(f'<p class="note">{esc(ui["per_currency"])}</p>')
    return "".join(parts), {"panels": panels_json}


def data_table(build: Build, panel: dict[str, Any], value_text: Callable[[Optional[Decimal]], str], cumulative: bool) -> str:
    ui = build.ui
    series = panel["series"]
    head = [f'<th scope="col">{esc(ui["category"])}</th>']
    for item in series:
        head.append(f'<th scope="col" class="num">{esc(item["label"] or ui["value"])}</th>')
        if cumulative:
            head.append(f'<th scope="col" class="num">{esc(ui["single"])}</th>')
    rows = []
    for index, category in enumerate(panel["categories"]):
        cells = [f'<th scope="row">{esc(category["label"])}</th>']
        for item in series:
            value = item["values"][index]
            raw = item.get("raw", item["values"])[index]
            cells.append(f'<td class="num" data-value="{esc("" if value is None else str(value))}">{esc(value_text(value))}</td>')
            if cumulative:
                cells.append(f'<td class="num" data-value="{esc("" if raw is None else str(raw))}">{esc(value_text(raw))}</td>')
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return (f'<details><summary>{esc(ui["data_table"])}</summary><div class="table-scroll"><table>'
            f'<thead><tr>{"".join(head)}</tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')


def render_widget(build: Build, widget: dict[str, Any], dashboard_id: str, here: str) -> tuple[str, dict[str, Any]]:
    ds, ui = build.ds, build.ui
    result = V.compute_widget(ds, widget, build.views)
    kind = widget["type"]
    title = f'<h2>{esc(widget["label"])}</h2>' if widget["label"] else ""
    if widget.get("description"):
        title += f'<p class="filter">{esc(widget["description"])}</p>'
    if kind not in ("richtext", "link", "table"):
        description = V.describe_filter(ds, widget["object"], widget.get("filter"), widget.get("me"))
        if description:
            title += f'<p class="filter">{esc(ui["filter"].format(text=description))}</p>'
    info: dict[str, Any] = {"id": widget["id"], "kind": kind}
    if kind == "richtext":
        content = f'<div class="rich-text">{markdown_html(widget["text"], build.wikilink(here))}</div>'
    elif kind == "link":
        url = safe_url(widget["url"]) or ""
        content = (f'<p>{external_link(url, ui["link_open"].format(url=url)) if url else esc(widget["url"])}</p>'
                   f'<p class="note">{esc(ui["link_note"])}</p>')
        info["url"] = url
    elif kind == "table":
        view = result.get("view")
        if view is None:
            content = f'<p class="note">{esc(result.get("error", ""))}</p>'
        else:
            label_field = build.dm.label_field(view["object"])
            refs = list(result["columns"])
            if not any(ref.key in (label_field, "crm_title") for ref in refs):
                refs.insert(0, V.resolve_field(build.dm, view["object"], label_field))
            head = "".join(f'<th scope="col">{esc(V.field_label(build.dm, view["object"], ref.key, build.language))}</th>' for ref in refs)
            rows = []
            for record in result["rows"]:
                cells = []
                for ref in refs:
                    if ref.key in (label_field, "crm_title"):
                        cells.append(f'<td><a href="{esc(build.record_href(here, record.id) or "#")}">{esc(record.data.get("crm_title") or record.id[:8])}</a></td>')
                    else:
                        cells.append(f"<td>{field_html(build, record, ref, here)}</td>")
                rows.append(f"<tr>{''.join(cells)}</tr>")
            content = f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            if result["hidden"]:
                content += f'<p class="note">{esc(build.counted("hidden_rows", result["hidden"]))}</p>'
            content += f'<p class="note"><a href="{esc(href_path(here, OUTPUT_DIR + "/views/" + view["id"] + ".html"))}">{esc(ui["open_view"])}</a></p>'
            info.update({"view": view["id"], "rows": [record.id for record in result["rows"]], "count": result["count"]})
    elif kind == "number":
        object_name = widget["object"]
        function = widget["aggregate"]["function"]
        field = widget["aggregate"].get("field") if function != "COUNT" else None
        values = result["values"]
        lines = []
        for value in values:
            if (widget["prefix"] or widget["suffix"]) and ds.is_money(object_name, function, field) and len(values) == 1 and value["value"] is not None:
                text = ds.format_number(value["value"], 2)
            else:
                # SUM and COUNT over nothing are 0; AVG, MIN and MAX of nothing have no value.
                text = ds.format_aggregate(object_name, function, field, value) or ("0" if function in ("SUM", "COUNT") else ui["no_value"])
            shown = f"{widget['prefix']}{text}{widget['suffix']}"
            lines.append(f'<p class="kpi{" long" if len(shown) > 11 else ""}">{esc(shown)}</p>')
        content = "".join(lines) + f'<p class="note">{esc(build.counted("records_count", result["count"]))}</p>'
        if len(values) > 1:
            content += f'<p class="note">{esc(ui["per_currency"])}</p>'
        info.update({"values": _values_json(values), "count": result["count"]})
    else:
        content, extra = render_chart_widget(build, widget, result, dashboard_id)
        info.update(extra)
        info["count"] = result["count"]
    return f'<section class="widget" style="{widget_style(widget)}">{title}{content}</section>', info


def render_dashboard(build: Build, dashboard: dict[str, Any]) -> bytes:
    ui = build.ui
    path = f"{OUTPUT_DIR}/dashboards/{dashboard['id']}.html"
    here = posixpath.dirname(path)
    sections, infos = [], []
    for widget in dashboard["widgets"]:
        html_text, info = render_widget(build, widget, dashboard["id"], here)
        sections.append(html_text)
        infos.append(info)
    body = f'<h1>{esc(dashboard["label"])}</h1>'
    if dashboard.get("description"):
        body += f'<p class="lead">{esc(dashboard["description"])}</p>'
    body += f'<p class="lead">{esc(build.counted("dashboard_lead", len(dashboard["widgets"])))}</p>'
    if dashboard.get("visibility") == "restricted":
        body += f'<p class="muted small">{esc(ui["restricted"].format(roles=", ".join(dashboard.get("roles") or [])))}</p>'
    body += f'<div class="grid12">{"".join(sections)}</div>'
    crumbs = [(ui["crm"], OUTPUT_DIR + "/index.html"), (ui["dashboards"], None), (dashboard["label"], None)]
    return build.page(path, kind="dashboard", title=dashboard["label"], crumbs=crumbs, body=body,
                      data={"dashboard": dashboard["id"], "widgets": infos})


# ---------------------------------------------------------------------------
# start page


def workflow_entries(target: Path) -> list[dict[str, Any]]:
    """Name and active state of every workflow definition; unreadable files are listed, not fatal."""
    root = target / WORKFLOWS_DIR
    entries = []
    if not root.is_dir():
        return entries
    for path in sorted(root.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            entries.append({"id": path.stem, "name": path.stem, "active": None, "readable": False})
            continue
        if not isinstance(document, dict):
            entries.append({"id": path.stem, "name": path.stem, "active": None, "readable": False})
            continue
        name = str(document.get("name") or document.get("label") or document.get("title") or path.stem)
        entries.append({"id": path.stem, "name": name, "active": _workflow_active(document), "readable": True})
    return entries


def _workflow_active(document: dict[str, Any]) -> Optional[bool]:
    if isinstance(document.get("active"), bool):
        return document["active"]
    status = document.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip().upper() == "ACTIVE"
    statuses = document.get("statuses")
    if isinstance(statuses, list):
        return "ACTIVE" in [str(item).upper() for item in statuses]
    versions = document.get("versions")
    if isinstance(versions, list):
        return any(isinstance(item, dict) and str(item.get("status") or "").upper() == "ACTIVE" for item in versions)
    return None


def render_index(build: Build) -> bytes:
    dm, ds, ui = build.dm, build.ds, build.ui
    path = f"{OUTPUT_DIR}/index.html"
    here = OUTPUT_DIR
    objects = []
    counts = {}
    for object_name in dm.objects:
        records = ds.records(object_name)
        trash = sum(1 for record in records if record.deleted)
        counts[object_name] = {"records": len(records) - trash, "trash": trash}
        inactive = dm.objects[object_name].get("active", True) is False
        note = build.counted("records_count", len(records) - trash)
        if trash:
            note += ", " + build.counted("trash_count", trash)
        if inactive:
            note += f" ({ui['inactive']})"
        objects.append(f'<li><a class="tile" href="{esc(href_path(here, build.object_page(object_name)))}">'
                       f'<strong>{esc(V.object_label(dm, object_name))}</strong><span>{esc(note)}</span></a></li>')
    body = [f'<h1>{esc(ui["home"])}</h1>']
    workspace = str(build.settings.get("workspace_name") or "")
    if workspace:
        body.append(f'<p class="lead">{esc(workspace)}</p>')
    body.append(f'<section class="panel"><h2>{esc(ui["objects"])}</h2><ul class="cards-grid">{"".join(objects)}</ul></section>')
    view_items = []
    for view in build.views:
        restricted = f' <span class="badge">{esc(", ".join(view.get("roles") or []))}</span>' if view.get("visibility") == "restricted" else ""
        view_items.append(f'<li><a href="{esc(href_path(here, OUTPUT_DIR + "/views/" + view["id"] + ".html"))}">{esc(view["label"])}</a> '
                          f'<span class="muted small">{esc(ui["type_" + view["type"]])} · {esc(V.object_label(dm, view["object"]))}</span>{restricted}</li>')
    body.append(f'<section class="panel"><h2>{esc(ui["views"])}</h2>'
                + (f'<ul class="links">{"".join(view_items)}</ul>' if view_items else f'<p class="muted">{esc(ui["none"])}</p>') + "</section>")
    dashboard_items = [f'<li><a href="{esc(href_path(here, OUTPUT_DIR + "/dashboards/" + item["id"] + ".html"))}">{esc(item["label"])}</a> '
                       f'<span class="muted small">{esc(build.counted("dashboard_lead", len(item["widgets"])))}</span></li>'
                       for item in build.dashboards]
    body.append(f'<section class="panel"><h2>{esc(ui["dashboards"])}</h2>'
                + (f'<ul class="links">{"".join(dashboard_items)}</ul>' if dashboard_items else f'<p class="muted">{esc(ui["none"])}</p>') + "</section>")
    workflow_items = []
    workflows = workflow_entries(build.target)
    for entry in workflows:
        if not entry["readable"]:
            state, css = ui["unreadable"], "warn"
        elif entry["active"] is None:
            state, css = ui["unknown"], "off"
        else:
            state, css = (ui["active"], "on") if entry["active"] else (ui["not_active"], "off")
        workflow_items.append(f'<li>{esc(entry["name"])} <span class="badge {css}">{esc(state)}</span></li>')
        if not entry["readable"]:
            build.warnings.append(f"{WORKFLOWS_DIR}/{entry['id']}.json is not readable JSON; listed without status")
    body.append(f'<section class="panel"><h2>{esc(ui["workflows"])}</h2>'
                + (f'<ul class="links">{"".join(workflow_items)}</ul>' if workflow_items else f'<p class="muted">{esc(ui["none"])}</p>') + "</section>")
    artifacts = artifact_files(build.target)
    drafts = [relative for relative in artifacts if relative.startswith(OUTBOX_DIR + "/") and not relative.endswith("/manifest.json")]
    stored = [relative for relative in artifacts if relative.startswith(FILES_DIR + "/")]
    draft_items = [f'<li><a href="{esc(href_path(here, relative))}">{esc(posixpath.basename(relative))}</a> '
                   f'<span class="muted small">{esc(posixpath.dirname(relative)[len(OUTBOX_DIR) + 1:])}</span></li>'
                   for relative in sorted(drafts, reverse=True)[:OUTBOX_SHOWN]]
    more = len(drafts) - len(draft_items)
    if more > 0:
        draft_items.append(f'<li class="muted">{esc(ui["more_files"].format(count=more))}</li>')
    size = sum((build.target / relative).stat().st_size for relative in stored)
    stored_note = f'<p class="muted small">{esc(ui["stored"].format(count=len(stored), size=human_size(size, build.language)))}</p>' if stored else ""
    body.append(f'<section class="panel"><h2>{esc(ui["outbox"])}</h2>'
                + (f'<p class="muted small">{esc(ui["outbox_note"])}</p><ul class="links">{"".join(draft_items)}</ul>' if draft_items
                   else f'<p class="muted">{esc(ui["none"])}</p>') + stored_note + "</section>")
    events = ds.events()
    recent = list(reversed(events[-RECENT_EVENTS:]))
    rows = []
    for event in recent:
        erased = bool(event.get("erased"))
        op = "erased" if erased else str(event.get("op") or "")
        object_name = str(event.get("object") or "")
        record_id = str(event.get("record_id") or "")
        if erased:
            record_html = f'<span class="muted">{esc(ui["erased_record"])}</span>'
        elif record_id in ds.titles:
            record_html = f'<a href="{esc(build.record_href(here, record_id))}">{esc(ds.title(record_id))}</a>'
        else:
            title = ((event.get("changes") or {}).get("crm_title") or [None])[0] if isinstance((event.get("changes") or {}).get("crm_title"), list) else None
            record_html = f'<span class="muted">{esc(title or record_id[:8])} ({esc(ui["gone"])})</span>'
        object_text = V.object_label(dm, object_name, False) if object_name in dm.objects else object_name
        rows.append(f'<tr><td>{esc(ds.format_instant(event.get("at")))}</td><td>{esc(event.get("actor") or "")}</td>'
                    f'<td>{esc(ui["ops"].get(op, op))}</td><td>{esc(object_text)}</td><td>{record_html}</td></tr>')
    table = (f'<div class="table-scroll"><table><thead><tr><th scope="col">{esc(ui["time"])}</th><th scope="col">{esc(ui["actor"])}</th>'
             f'<th scope="col">{esc(ui["action"])}</th><th scope="col">{esc(ui["object"])}</th><th scope="col">{esc(ui["record"])}</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>') if rows else f'<p class="muted">{esc(ui["none"])}</p>'
    body.append(f'<section class="panel"><h2>{esc(ui["activity"])}</h2>{table}</section>')
    data = {
        "objects": counts, "views": [view["id"] for view in build.views], "dashboards": [item["id"] for item in build.dashboards],
        "workflows": [{"name": entry["name"], "active": entry["active"]} for entry in workflows],
        "recent": [str(event.get("event_id") or "") for event in recent],
    }
    return build.page(path, kind="index", title=ui["home"], crumbs=[(ui["crm"], None)], body="".join(body), data=data)


# ---------------------------------------------------------------------------
# build and write


def build_files(target: Path, now: str) -> tuple[dict[str, bytes], Build]:
    """Render every page in memory; raises BuildInvalid for invalid views or dashboards."""
    build = Build(target, now)
    files: dict[str, bytes] = {f"{OUTPUT_DIR}/index.html": render_index(build)}
    for object_name in build.dm.objects:
        files[build.object_page(object_name)] = render_object(build, object_name)
    for view in build.views:
        files[f"{OUTPUT_DIR}/views/{view['id']}.html"] = render_view(build, view)
    for dashboard in build.dashboards:
        files[f"{OUTPUT_DIR}/dashboards/{dashboard['id']}.html"] = render_dashboard(build, dashboard)
    return files, build


def manifest_bytes(inputs_sha256: str, now: str, files: dict[str, bytes]) -> bytes:
    manifest = {
        "format": BUILD_FORMAT,
        "renderer": RENDERER,
        "inputs_sha256": inputs_sha256,
        "generated_at": now,
        "files": {path: hashlib.sha256(content).hexdigest() for path, content in sorted(files.items())},
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_output(target: Path, files: dict[str, bytes], manifest: bytes) -> dict[str, Any]:
    """Stage, then replace changed files one by one, remove stale files under graph/crm/, write the manifest last."""
    output = target / OUTPUT_DIR
    written, unchanged, removed, problems = 0, 0, [], []
    try:
        staging_root = tempfile.TemporaryDirectory(prefix=".lmwiki-crm-build-", dir=str(target.parent))
    except OSError:
        # A read-only parent (for example a synced library root) falls back to the system temp directory.
        staging_root = tempfile.TemporaryDirectory(prefix="lmwiki-crm-build-")
    with staging_root as temporary:
        staging = Path(temporary)
        for relative, content in files.items():
            staged = staging / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
        for relative in sorted(files):
            content = (staging / relative).read_bytes()
            destination = target / relative
            if destination.is_file() and destination.read_bytes() == content:
                unchanged += 1
                continue
            portable_io.atomic_write_bytes(destination, content)
            written += 1
    expected = set(files) | {MANIFEST_PATH}
    if output.is_dir():
        for path in sorted(output.rglob("*"), reverse=True):
            relative = path.relative_to(target).as_posix()
            try:
                if path.is_file() or path.is_symlink():
                    if relative not in expected:
                        path.unlink()
                        removed.append(relative)
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError as exc:
                problems.append(f"could not remove {relative}: {exc}")
    manifest_path = target / MANIFEST_PATH
    if not manifest_path.is_file() or manifest_path.read_bytes() != manifest:
        portable_io.atomic_write_bytes(manifest_path, manifest)
    return {"written": written, "unchanged": unchanged, "removed": removed, "problems": problems}


def run_build(target: Path, now: Optional[str] = None) -> dict[str, Any]:
    """Build graph/crm/ for an already locked wiki and return the report."""
    target = Path(target).expanduser().resolve()
    started = time.time()
    moment = parse_instant(now) if now else parse_instant(utc_now())
    if moment is None:
        raise CrmError(f"--now must be an ISO-8601 instant such as 2026-10-06T15:00:00Z, not {now!r}")
    now_text = format_instant(moment)
    inputs = expected_inputs_sha256(target)
    files, build = build_files(target, now_text)
    manifest = manifest_bytes(inputs, now_text, files)
    outcome = write_output(target, files, manifest)
    sizes = sorted(((len(content), path) for path, content in files.items()), reverse=True)
    total = sum(size for size, _path in sizes) + len(manifest)
    return {
        "state": "built",
        "output": OUTPUT_DIR + "/",
        "generated_at": now_text,
        "inputs_sha256": inputs,
        "files": len(files) + 1,
        "bytes": total,
        "largest": [{"path": path, "bytes": size} for size, path in sizes[:5]],
        "written": outcome["written"],
        "unchanged": outcome["unchanged"],
        "removed": outcome["removed"],
        "records": sum(len(build.ds.records(name)) for name in build.dm.objects),
        "views": len(build.views),
        "dashboards": len(build.dashboards),
        "warnings": build.warnings + outcome["problems"],
        "seconds": round(time.time() - started, 2),
        "next_step": "lint the wiki and release; graph/crm/ is part of the release",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True)
    parser.add_argument("--lock-token", required=True)
    parser.add_argument("--now", help="build moment (ISO-8601); relative filters and the 'as of' line use it")
    parser.add_argument("--check", action="store_true", help="only report whether graph/crm/ is fresh; writes nothing")
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    if args.check:
        try:
            errors = check_fresh(target)
        except Exception as exc:  # noqa: BLE001 - same JSON contract as the build
            print(json.dumps({"state": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps({"state": "fresh" if not errors else "stale", "errors": errors}, ensure_ascii=False, indent=2))
        return 0 if not errors else 3
    try:
        report = run_build(target, args.now)
    except BuildInvalid as exc:
        print(json.dumps({"state": "invalid", "writes": 0, "errors": exc.errors[:200], "error_count": len(exc.errors)},
                         ensure_ascii=False, indent=2))
        return 4
    except (CrmError, FilterError, OSError, ValueError) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001 - malformed data must still end in the JSON contract, not a traceback
        print(json.dumps({"state": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
