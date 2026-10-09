#!/usr/bin/env python3
"""Read e-mail files (.eml, .mbox) for the CRM import with the standard library only.

The parser never replaces characters silently: every text part is decoded
strictly with its declared charset, then with UTF-8, cp1252 and latin-1, and
each fallback is noted. HTML-only mails are turned into text with block
elements as line breaks, tables as Markdown tables and links as ``text (URL)``.
Attachments are only listed by name and size.

The module also holds the shared rules of the message import: bulk and
group detection, blocklist matching with subdomains, free e-mail domains,
registrable domains for company proposals, names from display names, thread
roots, and the credential redaction that runs before any record is planned.
"""

from __future__ import annotations

import email.errors
import email.header
import email.utils
import hashlib
import mailbox
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator, Optional

import secret_screen

REDACTED = "[credential removed]"
MAX_TEXT_CHARS = 100_000
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
EMAIL_RE = re.compile(r"^[^@\s<>\"(),;:]+@[^@\s<>\"(),;:]+\.[^@\s<>\"(),;:]+$")
MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")
INVISIBLE = re.compile("[­͏᠎​-‏⁠-⁤﻿]")
LINE_SEPARATORS = re.compile("[  \u0085\u000b\u000c]")
CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")

# Group addresses, bulk-mail headers and unsubscribe keywords
GROUP_PATTERN = re.compile(
    r"noreply|no-reply|do_not_reply|no\.reply|^(?:info@|contact@|hello@|support@|feedback@|service@|help@|invites@"
    r"|invite@|welcome@|alerts@|team@|notifications@|notification@|news@)"
)
UNSUBSCRIBE_KEYWORD = re.compile(r"(?<![a-z])(unsubscribe|unsub|opt[.\-_]?out)(?![a-z])")
BULK_LIST_HEADERS = ("list-unsubscribe", "list-id")
BULK_PRECEDENCE = ("bulk", "list", "junk")

# Reply and forward prefixes stripped when a thread key has to be derived from the subject.
REPLY_PREFIX = re.compile(
    r"^\s*(?:(?:re|aw|antw|antwort|fw|fwd|wg|wtr|sv|vs|rif|r|tr|enc|res|odp|vb|doorst|ynt|vl)\s*(?:\[\d+\]|\(\d+\))?\s*:\s*)+",
    re.IGNORECASE,
)

# A small built-in list of multi-label public suffixes; registrable domain = one label more.
MULTI_PART_SUFFIXES = frozenset("""
co.uk org.uk ac.uk gov.uk me.uk ltd.uk plc.uk net.uk sch.uk nhs.uk police.uk
com.au net.au org.au edu.au gov.au asn.au id.au
co.nz org.nz net.nz ac.nz govt.nz school.nz
co.jp ne.jp or.jp ac.jp go.jp ad.jp ed.jp gr.jp lg.jp
co.kr or.kr ne.kr go.kr ac.kr re.kr
com.br net.br org.br gov.br edu.br art.br
com.cn net.cn org.cn gov.cn edu.cn ac.cn
com.hk org.hk net.hk edu.hk gov.hk idv.hk
com.tw org.tw net.tw edu.tw gov.tw idv.tw
com.sg org.sg net.sg edu.sg gov.sg per.sg
com.my org.my net.my edu.my gov.my
co.in net.in org.in firm.in gen.in ind.in ac.in edu.in gov.in res.in
co.za org.za net.za gov.za ac.za web.za
co.il org.il net.il ac.il gov.il muni.il
com.mx org.mx net.mx gob.mx edu.mx
com.ar org.ar net.ar gob.ar edu.ar
com.tr org.tr net.tr gen.tr edu.tr gov.tr biz.tr
com.pl net.pl org.pl edu.pl gov.pl
co.at or.at ac.at gv.at
com.es org.es nom.es edu.es gob.es
com.pt org.pt edu.pt gov.pt
co.id or.id ac.id go.id web.id
com.ph net.ph org.ph gov.ph edu.ph
co.th ac.th go.th or.th in.th
com.vn net.vn org.vn edu.vn gov.vn
com.ua org.ua net.ua gov.ua edu.ua
co.ke or.ke ac.ke go.ke
com.ng org.ng gov.ng edu.ng
com.eg edu.eg gov.eg
com.sa edu.sa gov.sa
com.pk org.pk edu.pk gov.pk
com.co org.co edu.co gov.co
com.pe org.pe edu.pe gob.pe
com.uy edu.uy gub.uy
co.ma co.cr co.ve com.ve
""".split())

CREDENTIAL_PARAMETERS = (
    "token", "pwd", "password", "pass", "code", "key", "sig", "signature", "auth", "access_token",
    "passwd", "passcode", "secret", "client_secret", "api_key", "apikey", "id_token", "refresh_token", "jwt", "otp",
)
QUERY_CREDENTIAL = re.compile(
    r"(?P<lead>[?&;](?P<name>" + "|".join(re.escape(name) for name in CREDENTIAL_PARAMETERS) + r")=)(?P<value>[^&#\s\"'<>()\[\]]+)",
    re.IGNORECASE,
)
CREDENTIAL_WORDS = (
    r"passwort|kennwort|passcode|kenncode|password|passwd|pin|zugangscode|einmalpasswort|einmal-passwort"
    r"|access code|sicherheitscode|security code|bestätigungscode|verification code|einmalcode|einmal-code|otp"
)
CREDENTIAL_LINE = re.compile(
    r"^(?P<prefix>[^\n]{0,60}?\b(?:" + CREDENTIAL_WORDS + r")\b[^\n:=]{0,40}[:=])(?P<value>[^\S\n]*\S[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
# "Das Passwort lautet Sommer2024": no colon, the value follows a verb; it needs a digit or a symbol.
CREDENTIAL_PHRASE = re.compile(
    r"(?P<prefix>\b(?:" + CREDENTIAL_WORDS + r")\b[^\n:=]{0,20}?\b(?:lautet|ist|is|heißt|heisst)\b[^\S\n]+)"
    r"(?P<value>(?=[^\s]*[0-9!@#$%^&_+=?])[^\s*•][^\s]{3,})",
    re.IGNORECASE,
)
CREDENTIAL_HEADING = re.compile(
    r"^[^\n]{0,60}?\b(?:" + CREDENTIAL_WORDS + r")\b[^\n:=]{0,40}[:=][^\S\n]*$", re.IGNORECASE
)
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)", re.DOTALL
)
URL_USERINFO = re.compile(r"\b(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/\s:@<>\"'`]+:[^/\s:@<>\"'`]+)@", re.IGNORECASE)


class MailError(ValueError):
    """An e-mail file cannot be read."""


# ---------------------------------------------------------------------------
# decoding


def decode_bytes(data: bytes, declared: Optional[str] = None) -> tuple[str, Optional[str]]:
    """Strict decoding with the declared charset, then UTF-8, cp1252 and latin-1.

    Returns the text and a note when a fallback was needed; nothing is ever
    replaced by U+FFFD.
    """
    tried: list[str] = []
    chain = []
    if declared:
        chain.append(declared.strip().strip('"').lower())
    chain.extend(["utf-8", "cp1252", "latin-1"])
    for charset in chain:
        if charset in tried:
            continue
        tried.append(charset)
        try:
            text = data.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
        if declared and charset != tried[0]:
            return text, f"declared charset {declared} does not fit the content; read as {charset}"
        if not declared and charset not in {"utf-8", "us-ascii", "ascii"}:
            return text, f"no charset declared; read as {charset}"
        return text, None
    return data.decode("latin-1"), "read as latin-1"  # pragma: no cover - latin-1 never fails


def fix_surrogates(text: str) -> tuple[str, Optional[str]]:
    """Header text from raw 8-bit bytes arrives with surrogate escapes; decode those bytes properly."""
    if not re.search("[\udc80-\udcff]", text):
        return text, None
    raw = text.encode("utf-8", "surrogateescape")
    decoded, note = decode_bytes(raw, None)
    return decoded, ("header with raw 8-bit bytes; " + note) if note else None


def unfold(value: str) -> str:
    return re.sub(r"\r?\n(?=[ \t])", "", value).replace("\r", "").replace("\n", " ")


def decode_header_value(raw: str, notes: Optional[list[str]] = None) -> str:
    """Decode RFC 2047 encoded words strictly, with the same fallbacks as bodies."""
    text, note = fix_surrogates(unfold(raw))
    if note and notes is not None:
        notes.append(note)
    if "=?" not in text:
        return text.strip()
    try:
        parts = email.header.decode_header(text)
    except (email.errors.HeaderParseError, ValueError):
        return text.strip()
    pieces = []
    for value, charset in parts:
        if isinstance(value, str):
            pieces.append(value)
        elif charset:
            decoded, part_note = decode_bytes(value, charset)
            if part_note and notes is not None:
                notes.append("header: " + part_note)
            pieces.append(decoded)
        else:
            pieces.append(value.decode("raw-unicode-escape"))
    return "".join(pieces).strip()


def single_line(value: str, limit: int = 500) -> str:
    """Text for one frontmatter value: no control characters, no line breaks."""
    text = LINE_SEPARATORS.sub(" ", value)
    text = CONTROL.sub("", INVISIBLE.sub("", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def clean_text(value: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, Optional[str]]:
    """Body text: normalized line ends, no control characters, record markers neutralized."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = LINE_SEPARATORS.sub("\n", text)
    text = CONTROL.sub("", INVISIBLE.sub("", text)).replace(" ", " ")
    text = re.sub(r"<!--(\s*/?\s*crm:richtext)", r"&lt;!--\1", text)
    lines = [line.rstrip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
    note = None
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n\n[...]"
        note = f"text shortened to {limit} characters"
    return text, note


# ---------------------------------------------------------------------------
# HTML to text


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP_TAGS = {"script", "style", "head", "title", "template", "noscript", "svg", "object", "xml", "o:p"}
PARAGRAPH_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote", "pre", "dl", "hr", "figure", "address"}
LINE_TAGS = {"div", "li", "tr", "dt", "dd", "section", "article", "header", "footer", "main", "nav", "aside", "center",
             "form", "fieldset", "caption", "figcaption", "details", "summary", "tbody", "thead", "tfoot", "td", "th", "body", "html"}


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: Optional[dict[str, str]] = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []


class _TreeBuilder(HTMLParser):
    """A tolerant DOM for mail HTML: unclosed and stray tags are common there."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def _close_open(self, names: set[str], barriers: set[str]) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[index].tag
            if tag in barriers:
                return
            if tag in names:
                del self.stack[index:]
                return

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag == "li":
            self._close_open({"li"}, {"ul", "ol", "table"})
        elif tag in {"td", "th"}:
            self._close_open({"td", "th"}, {"tr", "table"})
        elif tag == "tr":
            self._close_open({"tr"}, {"table"})
        elif tag in {"thead", "tbody", "tfoot"}:
            self._close_open({"thead", "tbody", "tfoot"}, {"table"})
        elif tag in {"dt", "dd"}:
            self._close_open({"dt", "dd"}, {"dl"})
        elif tag in PARAGRAPH_TAGS | LINE_TAGS and self.stack[-1].tag == "p":
            self.stack.pop()
        node = Node(tag, {key.lower(): (value or "") for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        node = Node(tag.lower(), {key.lower(): (value or "") for key, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _hidden(node: Node) -> bool:
    style = node.attrs.get("style", "").replace(" ", "").lower()
    return "display:none" in style or "mso-hide:all" in style or "hidden" in node.attrs


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.current: list[str] = []
        self.breaks = 0

    def text(self, value: str) -> None:
        if not value:
            return
        if not self.current:
            value = value.lstrip()
            if not value:
                return
            if self.lines or self.breaks:
                self.lines.extend([""] * max(0, self.breaks - 1))
            self.breaks = 0
        elif self.current[-1].endswith(" ") and value.startswith(" "):
            value = value.lstrip()
        self.current.append(value)

    def line_break(self, minimum: int = 1) -> None:
        if self.current:
            self.lines.append("".join(self.current).rstrip())
            self.current = []
            self.breaks = minimum
        else:
            self.breaks = max(self.breaks, minimum)

    def raw_lines(self, lines: list[str]) -> None:
        self.line_break(2)
        if self.lines:
            self.lines.extend([""] * max(0, self.breaks - 1))
        self.lines.extend(lines)
        self.breaks = 2

    def result(self) -> str:
        self.line_break()
        return "\n".join(self.lines).strip("\n")


def _collapse(value: str) -> str:
    return re.sub(r"[ \t\r\n\f ]+", " ", INVISIBLE.sub("", value))


def _inline_text(node: Node) -> str:
    out = _Out()
    _render_children(node, out, list_stack=[])
    return re.sub(r"\s*\n\s*", " ", out.result()).strip()


def _is_layout_table(node: Node) -> bool:
    rows = _table_rows(node)
    if not rows or max(len(row) for row in rows) < 2:
        return True

    def nested(element: Node) -> bool:
        for child in element.children:
            if isinstance(child, Node) and (child.tag == "table" or nested(child)):
                return True
        return False

    return any(nested(cell) for row in rows for cell in row)


def _table_rows(node: Node) -> list[list[Node]]:
    rows: list[list[Node]] = []

    def walk(element: Node) -> None:
        for child in element.children:
            if not isinstance(child, Node):
                continue
            if child.tag == "tr":
                rows.append([cell for cell in child.children if isinstance(cell, Node) and cell.tag in {"td", "th"}])
            elif child.tag in {"thead", "tbody", "tfoot"}:
                walk(child)

    walk(node)
    return [row for row in rows if row]


def _render_table(node: Node, out: _Out) -> None:
    rows = [[_inline_text(cell).replace("|", "\\|") for cell in row] for row in _table_rows(node)]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    out.raw_lines(lines)


def _render_children(node: Node, out: _Out, list_stack: list) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.text(_collapse(child))
        else:
            _render(child, out, list_stack)


def _render(node: Node, out: _Out, list_stack: list) -> None:
    tag = node.tag
    if tag in SKIP_TAGS or _hidden(node):
        return
    if tag == "br":
        out.line_break(1)
        return
    if tag == "hr":
        out.raw_lines(["---"])
        return
    if tag == "img":
        return
    if tag == "pre":
        text = "".join(child if isinstance(child, str) else _inline_text(child) for child in node.children)
        out.raw_lines([line.rstrip() for line in text.strip("\n").split("\n")])
        return
    if tag == "table":
        if _is_layout_table(node):
            out.line_break(1)
            for row in _table_rows(node):
                for cell in row:
                    _render_children(cell, out, list_stack)
                    out.line_break(1)
            out.line_break(1)
        else:
            _render_table(node, out)
        return
    if tag == "blockquote":
        inner = _Out()
        _render_children(node, inner, list_stack)
        text = inner.result()
        if text:
            out.raw_lines([("> " + line).rstrip() for line in text.split("\n")])
        return
    if tag == "a":
        inner = _inline_text(node)
        href = node.attrs.get("href", "").strip()
        if href.lower().startswith("mailto:"):
            address = href[7:].split("?", 1)[0]
            label = inner if not inner or address.lower() in inner.lower() else f"{inner} ({address})"
            out.text(label or address)
        elif href.lower().startswith(("http://", "https://")) and href not in inner:
            out.text(f"{inner} ({href})" if inner else href)
        else:
            out.text(inner)
        return
    if tag in {"ul", "ol"}:
        out.line_break(2)
        list_stack.append([tag, 0])
        _render_children(node, out, list_stack)
        list_stack.pop()
        out.line_break(2)
        return
    if tag == "li":
        out.line_break(1)
        if list_stack:
            list_stack[-1][1] += 1
            marker = f"{list_stack[-1][1]}. " if list_stack[-1][0] == "ol" else "- "
            indent = "  " * (len(list_stack) - 1)
        else:
            marker, indent = "- ", ""
        out.text(indent + marker)
        _render_children(node, out, list_stack)
        out.line_break(1)
        return
    if re.fullmatch(r"h[1-6]", tag):
        out.line_break(2)
        out.text("#" * int(tag[1]) + " ")
        _render_children(node, out, list_stack)
        out.line_break(2)
        return
    if tag in PARAGRAPH_TAGS:
        out.line_break(2)
        _render_children(node, out, list_stack)
        out.line_break(2)
        return
    if tag in LINE_TAGS:
        out.line_break(1)
        _render_children(node, out, list_stack)
        out.line_break(1)
        return
    _render_children(node, out, list_stack)


def html_to_text(html: str) -> str:
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except (AssertionError, ValueError):  # malformed markup; keep what was parsed
        pass
    out = _Out()
    try:
        _render(builder.root, out, [])
        text = out.result()
    except RecursionError:
        # Pathologically deep markup: fall back to tag removal with line breaks at blocks.
        text = re.sub(r"(?i)<(?:br|/p|/div|/tr|/li|/h[1-6])[^>]*>", "\n", html)
        text = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", text)
        text = "\n".join(_collapse(line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.split("\n"))).strip()


# ---------------------------------------------------------------------------
# messages


@dataclass
class Address:
    email: str
    name: str = ""


@dataclass
class Attachment:
    name: str
    content_type: str
    size: int


@dataclass
class ParsedMail:
    label: str
    sha256: str
    message_id: str = ""
    message_id_generated: bool = False
    subject: str = ""
    date: Optional[datetime] = None
    sender: list[Address] = field(default_factory=list)
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    bcc: list[Address] = field(default_factory=list)
    reply_to: list[Address] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    in_reply_to: list[str] = field(default_factory=list)
    headers: dict[str, list[str]] = field(default_factory=dict)
    text: str = ""
    text_source: str = "none"
    attachments: list[Attachment] = field(default_factory=list)
    calendars: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def from_address(self) -> str:
        return self.sender[0].email if self.sender else ""

    def addresses(self) -> list[Address]:
        """Every participant once: from, to, cc, bcc, then reply-to other than the sender."""
        seen: set[str] = set()
        result = []
        for address in self.sender + self.to + self.cc + self.bcc + self.reply_to:
            if address.email not in seen:
                seen.add(address.email)
                result.append(address)
        return result

    def header(self, name: str) -> str:
        values = self.headers.get(name.lower()) or []
        return values[0] if values else ""


def is_outlook_msg(data: bytes) -> bool:
    return data[:8] == OLE_SIGNATURE


def parse_addresses(raw_values: Iterable[str], notes: list[str]) -> list[Address]:
    result: list[Address] = []
    for raw in raw_values:
        text, note = fix_surrogates(unfold(raw))
        if note:
            notes.append(note)
        for name, address in email.utils.getaddresses([text]):
            address = address.strip().strip("<>").lower()
            if not address:
                continue
            if not EMAIL_RE.fullmatch(address):
                notes.append(f"ignored invalid address {single_line(address, 80)!r}")
                continue
            display = single_line(decode_header_value(name, notes), 200).strip('"\' ')
            if address not in {item.email for item in result}:
                result.append(Address(address, display))
    return result


def parse_message_ids(value: str) -> list[str]:
    found = MESSAGE_ID_RE.findall(value or "")
    if found:
        return found
    tokens = [token.strip("<>") for token in (value or "").split() if "@" in token]
    return [f"<{token}>" for token in tokens]


def parse_date(value: str, notes: list[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        notes.append("the Date header cannot be read")
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _leaf_parts(part: Message) -> Iterator[Message]:
    if part.get_content_type() == "message/rfc822":
        yield part
        return
    if part.is_multipart():
        payload = part.get_payload()
        for sub in payload if isinstance(payload, list) else []:
            yield from _leaf_parts(sub)
        return
    yield part


def _filename(part: Message, notes: list[str]) -> str:
    try:
        name = part.get_filename() or ""
    except (ValueError, LookupError, TypeError):
        name = ""
        notes.append("an attachment name cannot be decoded")
    return single_line(decode_header_value(name, notes), 200) if name else ""


def _disposition(part: Message) -> str:
    try:
        return (part.get_content_disposition() or "").lower()
    except (ValueError, TypeError):
        return ""


def _payload_bytes(part: Message) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except (ValueError, TypeError, LookupError):
        payload = None
    if isinstance(payload, bytes):
        return payload
    try:
        return part.as_bytes()
    except Exception:  # noqa: BLE001 - a broken part still counts as an attachment
        return b""


def _part_text(part: Message, notes: list[str], what: str) -> str:
    charset = part.get_param("charset")
    if isinstance(charset, tuple):
        charset = charset[2]
    text, note = decode_bytes(_payload_bytes(part), str(charset) if charset else None)
    if note:
        notes.append(f"{what}: {note}")
    return text


def parse_mail(data: bytes, label: str, envelope_date: Optional[datetime] = None) -> ParsedMail:
    """Parse one RFC 5322 message; Outlook .msg data is refused."""
    if is_outlook_msg(data):
        raise MailError("Outlook .msg file; export the message as .eml and import that file")
    parsed = ParsedMail(label=label, sha256=hashlib.sha256(data).hexdigest())
    notes = parsed.notes
    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
    except Exception as exc:  # noqa: BLE001 - the email package raises many types on broken input
        raise MailError(f"the message cannot be parsed: {exc}") from exc
    raw_headers: dict[str, list[str]] = {}
    for name, value in message.raw_items():
        raw_headers.setdefault(name.lower(), []).append(str(value))
    if not raw_headers:
        raise MailError("no mail headers found")
    parsed.headers = {name: [single_line(decode_header_value(value, notes), 2000) for value in values] for name, values in raw_headers.items()}
    parsed.subject = single_line(decode_header_value((raw_headers.get("subject") or [""])[0], notes))
    parsed.sender = parse_addresses(raw_headers.get("from", []), notes)
    parsed.to = parse_addresses(raw_headers.get("to", []), notes)
    parsed.cc = parse_addresses(raw_headers.get("cc", []), notes)
    parsed.bcc = parse_addresses(raw_headers.get("bcc", []), notes)
    sender_emails = {address.email for address in parsed.sender}
    parsed.reply_to = [address for address in parse_addresses(raw_headers.get("reply-to", []), notes) if address.email not in sender_emails]
    parsed.date = parse_date(parsed.header("date"), notes) or envelope_date
    if parsed.date is None:
        notes.append("the message has no readable date")
    ids = parse_message_ids(parsed.header("message-id"))
    parsed.references = parse_message_ids(parsed.header("references"))
    parsed.in_reply_to = parse_message_ids(parsed.header("in-reply-to"))

    plain: list[str] = []
    html: list[str] = []
    for part in _leaf_parts(message):
        content_type = part.get_content_type()
        disposition = _disposition(part)
        name = _filename(part, notes)
        if content_type in {"text/calendar", "application/ics"} or name.lower().endswith((".ics", ".ical")):
            parsed.calendars.append(_part_text(part, notes, "calendar part"))
            continue
        if content_type == "message/rfc822":
            inner = part.get_payload()
            inner_message = inner[0] if isinstance(inner, list) and inner else None
            subject = ""
            if isinstance(inner_message, Message):
                subject = single_line(decode_header_value(str(inner_message.get("subject", "") or ""), notes), 200)
            parsed.attachments.append(Attachment(name or (subject + ".eml" if subject else "message.eml"), content_type, len(_payload_bytes(part))))
            continue
        is_body = content_type in {"text/plain", "text/html"} and disposition != "attachment" and not name
        if is_body and content_type == "text/plain":
            plain.append(_part_text(part, notes, "text part"))
        elif is_body:
            html.append(_part_text(part, notes, "HTML part"))
        elif name or disposition == "attachment":
            parsed.attachments.append(Attachment(name or "(unnamed)", content_type, len(_payload_bytes(part))))
    if plain and any(text.strip() for text in plain):
        parsed.text_source = "plain"
        raw_text = "\n\n".join(text.strip("\n") for text in plain if text.strip())
    elif html:
        parsed.text_source = "html"
        raw_text = "\n\n".join(html_to_text(text) for text in html)
    else:
        raw_text = ""
    parsed.text, note = clean_text(raw_text)
    if note:
        notes.append(note)
    parsed.notes[:] = list(dict.fromkeys(notes))
    if ids:
        parsed.message_id = ids[0]
    else:
        seed = "|".join([
            parsed.header("date"), parsed.from_address, ",".join(a.email for a in parsed.to), parsed.subject,
            hashlib.sha256(parsed.text.encode("utf-8")).hexdigest(),
        ])
        parsed.message_id = f"<generated-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}@lmwiki.invalid>"
        parsed.message_id_generated = True
        notes.append("the message has no Message-ID; a stable ID was derived from date, sender, recipients, subject and text")
    return parsed


def iter_mbox(path: Path) -> Iterator[tuple[int, bytes, Optional[datetime]]]:
    """(position, message bytes, envelope date) for each message of an mbox file."""
    box = mailbox.mbox(str(path), create=False)
    try:
        for position, key in enumerate(box.iterkeys(), 1):
            data = box.get_bytes(key)
            envelope = None
            try:
                from_line = box.get_message(key).get_from() or ""
                parts = from_line.split(None, 1)
                if len(parts) == 2:
                    stamp = email.utils.parsedate_tz(parts[1])
                    if stamp:
                        # An envelope date without a zone is read as UTC, never as machine time.
                        envelope = datetime(*stamp[:6], tzinfo=timezone.utc) - timedelta(seconds=stamp[9] or 0)
            except (ValueError, TypeError, OverflowError, KeyError):
                envelope = None
            yield position, data, envelope
    finally:
        box.close()


def detect_kind(path: Path, head: bytes) -> str:
    """eml, mbox, ics, msg or unknown, by signature first and extension second."""
    suffix = path.suffix.lower()
    if head[:8] == OLE_SIGNATURE:
        return "msg"
    sample = head.lstrip(b"\xef\xbb\xbf \t\r\n")[:2048]
    if suffix in {".ics", ".ical", ".ifb"} or sample.upper().startswith(b"BEGIN:VCALENDAR"):
        return "ics"
    if suffix in {".mbox", ".mbx"} or sample.startswith(b"From "):
        return "mbox"
    if suffix == ".eml" or re.match(rb"^[A-Za-z][A-Za-z0-9-]*:[^\r\n]*\r?\n", sample):
        return "eml"
    if suffix == ".msg":
        return "msg"
    return "unknown"


# ---------------------------------------------------------------------------
# rules shared with the campaign helper


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[1].lower().rstrip(".") if "@" in address else ""


def registrable_domain(domain: str) -> str:
    labels = [label for label in domain.lower().strip(".").split(".") if label]
    if len(labels) < 2 or re.fullmatch(r"[\d.]+", domain):
        return domain.lower()
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    if ".".join(labels[-2:]) in MULTI_PART_SUFFIXES:
        return ""
    return ".".join(labels[-2:])


def company_name_from_domain(domain: str) -> str:
    registrable = registrable_domain(domain)
    label = registrable.split(".", 1)[0] if registrable else ""
    return label[:1].upper() + label[1:]


def is_free_email(address: str, free_domains: Iterable[str]) -> bool:
    domain = domain_of(address)
    free = {item.strip().lower().lstrip("@") for item in free_domains if isinstance(item, str)}
    return bool(domain) and (domain in free or registrable_domain(domain) in free)


def is_blocklisted(address: str, blocklist: Iterable[str], own: Iterable[str] = ()) -> bool:
    """Blocklist rule: an exact address or @domain including subdomains; own addresses never count."""
    normalized = address.strip().lower()
    if not normalized or normalized in {item.strip().lower() for item in own}:
        return False
    domain = domain_of(normalized)
    for raw in blocklist:
        if not isinstance(raw, str) or not raw.strip():
            continue
        item = raw.strip().lower()
        if item.startswith("@") or ("@" not in item and "." in item):
            bare = item.lstrip("@")
            if domain == bare or domain.endswith("." + bare):
                return True
        elif normalized == item:
            return True
    return False


def is_group_address(address: str) -> bool:
    return bool(GROUP_PATTERN.search(address.lower()))


def is_unsubscribe_address(address: str) -> bool:
    normalized = address.lower()
    local = normalized.rsplit("@", 1)[0]
    if UNSUBSCRIBE_KEYWORD.search(local):
        return True
    domain = domain_of(normalized)
    registrable = registrable_domain(domain)
    if not registrable or domain == registrable:
        return False
    subdomain = domain[: -len(registrable) - 1]
    return any(UNSUBSCRIBE_KEYWORD.search(label) for label in subdomain.split("."))


def bulk_headers(mail: ParsedMail) -> list[str]:
    """Header names that mark a mailing list or automated mail (bulk-mail rule)."""
    found = []
    for name in BULK_LIST_HEADERS:
        if any(value.strip() for value in mail.headers.get(name, [])):
            found.append(name)
    if any(value.strip().lower() in BULK_PRECEDENCE for value in mail.headers.get("precedence", [])):
        found.append("precedence")
    if any(value.strip() and value.strip().lower() != "no" for value in mail.headers.get("auto-submitted", [])):
        found.append("auto-submitted")
    return found


def person_name(display_name: str, address: str) -> tuple[str, str]:
    """First and last name from a display name, falling back to the address (common rules)."""

    def from_local(local: str) -> tuple[str, str]:
        parts = [part for part in local.split("+", 1)[0].split(".") if part]
        return (parts[0] if parts else "", " ".join(parts[1:]))

    def from_display(name: str) -> tuple[str, str]:
        cleaned = re.sub(r"^['\"]+|['\"]+$", "", name.strip()).strip()
        if not cleaned or "@" in cleaned:
            return "", ""
        strip_tag = lambda value: re.sub(r":[^:]+$", "", value).strip()  # noqa: E731
        comma = re.match(r"^([^,]+),\s*(.+)$", cleaned)
        if comma:
            last = comma.group(1).strip()
            first = re.sub(r"\s+", " ", re.sub(r"\s*,\s*", " ", comma.group(2).strip()))
            if first and last:
                return strip_tag(first), strip_tag(last)
        tokens = cleaned.split()
        head, dot_tail = from_local(tokens[0])
        rest = " ".join(tokens[1:])
        if not dot_tail:
            return strip_tag(head), strip_tag(rest)
        last = rest if rest.lower().startswith(dot_tail.lower()) else f"{dot_tail} {rest}".strip()
        return strip_tag(head), strip_tag(last)

    display_first, display_last = from_display(display_name)
    local_first, local_last = from_local(address.rsplit("@", 1)[0])
    first = display_first or local_first
    last = display_last or local_last
    capitalize = lambda value: value[:1].upper() + value[1:]  # noqa: E731
    return single_line(capitalize(first), 100), single_line(capitalize(last), 100)


def normalize_subject(subject: str) -> str:
    text = subject or ""
    while True:
        stripped = REPLY_PREFIX.sub("", text)
        if stripped == text:
            break
        text = stripped
    return re.sub(r"\s+", " ", text).strip().casefold()


def thread_root(mail: ParsedMail) -> str:
    """References root, then In-Reply-To, then the own Message-ID (as usual), else a stable hash."""
    if mail.references:
        return mail.references[0]
    if mail.in_reply_to:
        return mail.in_reply_to[0]
    if not mail.message_id_generated:
        return mail.message_id
    participants = ",".join(sorted(address.email for address in mail.addresses()))
    seed = normalize_subject(mail.subject) + "|" + participants
    return "thread-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# credentials


@dataclass
class Redaction:
    line: int
    kind: str


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def redact(text: str) -> tuple[str, list[Redaction]]:
    """Replace credential values by [credential removed]; return the text and what was removed.

    Removed: private key blocks, user:password in URLs, JSON Web Tokens, URL query
    parameters named like credentials (token, pwd, password, pass, code, key, sig,
    signature, auth, access_token and a few more), token shapes known to
    secret_screen, and the value after 'Passwort:', 'Kennwort:', 'Passcode:',
    'Kenncode:', 'PIN:' and similar labels, also when the value stands alone on the
    next line. The removed values never appear in a report.
    """
    if not text:
        return text, []
    findings: list[Redaction] = []

    def substitute(pattern: "re.Pattern[str]", value: str, kind_of, replacement) -> str:
        result = []
        position = 0
        for match in pattern.finditer(value):
            kind = kind_of(match)
            if kind is None:
                continue
            findings.append(Redaction(_line_of(value, match.start()), kind))
            result.append(value[position:match.start()])
            result.append(replacement(match))
            position = match.end()
        result.append(value[position:])
        return "".join(result)

    text = substitute(PRIVATE_KEY_BLOCK, text, lambda m: "private-key", lambda m: REDACTED)
    text = substitute(URL_USERINFO, text, lambda m: None if REDACTED in m.group(0) or secret_screen.is_placeholder("url-credentials", m.group(0)) else "url-credentials",
                      lambda m: m.group("scheme") + REDACTED + "@")
    text = substitute(JWT, text, lambda m: "json-web-token", lambda m: REDACTED)
    text = substitute(QUERY_CREDENTIAL, text, lambda m: None if m.group("value") == REDACTED else f"url-parameter:{m.group('name').lower()}",
                      lambda m: m.group("lead") + REDACTED)
    for kind, pattern in secret_screen.PATTERNS:
        if kind in {"private-key", "json-web-token", "url-credentials", "credential-phrase"}:
            continue
        text = substitute(pattern, text, lambda m, kind=kind: None if secret_screen.is_placeholder(kind, m.group(0)) else kind, lambda m: REDACTED)
    text = substitute(CREDENTIAL_LINE, text, lambda m: None if m.group("value").strip() == REDACTED else "credential-line",
                      lambda m: m.group("prefix") + " " + REDACTED)
    text = substitute(CREDENTIAL_PHRASE, text, lambda m: None if REDACTED in m.group(0) else "credential-line",
                      lambda m: m.group("prefix") + REDACTED)
    lines = text.split("\n")
    for index, line in enumerate(lines[:-1]):
        if CREDENTIAL_HEADING.match(line):
            for following in range(index + 1, min(index + 3, len(lines))):
                candidate = lines[following].strip()
                if not candidate:
                    continue
                if candidate != REDACTED and len(candidate) <= 128 and not re.search(r"\s", candidate):
                    lines[following] = lines[following].replace(candidate, REDACTED)
                    findings.append(Redaction(following + 1, "credential-line"))
                break
    findings.sort(key=lambda item: item.line)
    return "\n".join(lines), findings


def screen(text: str) -> list[tuple[int, str]]:
    """secret_screen findings (line, kind) that remain after redaction."""
    return secret_screen.scan_text(text or "")
