#!/usr/bin/env python3
"""End-to-end tests for crm_tabular.py, crm_import.py and crm_export.py on throwaway test wikis.

Run:  python3 tests/import-export/test_import_export.py [--skip-performance] [--rows N]
Prints one line per check and ends with 'ALL OK' or the list of failures.
Test wikis live under test-wikis/import-export/, files and plans under test-wikis/import-export/.work/.
"""
import io
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS / "tools"))
from harness import SCRIPTS, Wiki, base_wiki  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
import crm_tabular  # noqa: E402
from crm_contract import RecordStore, load_datamodel  # noqa: E402

BASE = WS / "test-wikis" / "import-export"
WORK = BASE / ".work"
TOKENS = BASE / ".tokens"
FAILURES = []
MEASUREMENTS = {}


def check(name, condition, detail=""):
    if condition:
        print(f"ok    {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        FAILURES.append(f"{name}: {detail}")
    return condition


class Session:
    """One test wiki under the wiki lock; helpers are called directly with --lock-token."""

    def __init__(self, name):
        self.target = BASE / name
        if self.target.exists():
            shutil.rmtree(self.target)
        shutil.copytree(base_wiki(), self.target)
        self.wiki = Wiki(self.target, TOKENS)
        state = self.wiki.acquire("import-export-test").get("state")
        assert state == "held", state
        self.token = self.wiki.token_file.read_text(encoding="utf-8").strip()
        result = self.wiki.locked("crm_init.py", "--target", self.target)
        assert result.get("state") == "initialized", result
        self.work = WORK / name
        if self.work.exists():
            shutil.rmtree(self.work)
        self.work.mkdir(parents=True)

    def run(self, script, command, *args, expect=None):
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / script), command, "--target", str(self.target), "--lock-token", self.token, *map(str, args)],
            capture_output=True, text=True,
        )
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError:
            data = {"_raw": completed.stdout[-3000:], "_stderr": completed.stderr[-3000:]}
        if expect is not None and completed.returncode not in expect:
            print(f"      unexpected exit {completed.returncode} for {script} {command}: {completed.stdout[-1500:]} {completed.stderr[-1500:]}")
        return completed.returncode, data, completed.stdout

    def file(self, name, content, encoding="utf-8"):
        path = self.work / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode(encoding))
        return path

    def plan(self, file, object_name, *extra, match_key="id", mode="upsert", name="plan.json", mapping=None):
        plan_path = self.work / name
        args = ["--file", file, "--object", object_name, "--match-key", match_key, "--mode", mode,
                "--actor", "agent/import-test", "--origin-ref", Path(file).name, "--output", plan_path]
        args += ["--mapping-file", mapping] if mapping else ["--auto-mapping"]
        code, report, raw = self.run("crm_import.py", "plan", *args, *extra)
        return code, report, raw, plan_path

    def apply(self, plan_path, report):
        return self.run("crm_import.py", "apply", "--plan-file", plan_path, "--expect-plan-sha256", report["plan_sha256"])

    def import_file(self, file, object_name, *extra, **kwargs):
        code, report, raw, plan_path = self.plan(file, object_name, *extra, **kwargs)
        if code != 0:
            return code, report, raw, None
        apply_code, result, _ = self.apply(plan_path, report)
        return apply_code, report, raw, result

    def transact(self, operations, name):
        return self.wiki.transact({"actor": "agent/import-test", "origin": {"kind": "manual"}, "operations": operations}, self.work / name)

    def records(self, object_name):
        datamodel = load_datamodel(self.target)
        return list(RecordStore(self.target, datamodel).records(object_name).values())

    def find(self, object_name, **criteria):
        return [record for record in self.records(object_name) if all(record.data.get(key) == value for key, value in criteria.items())]

    def close(self):
        self.wiki.release()


# ---------------------------------------------------------------------------
# helpers for building files


def xlsx_bytes(rows, *, date1904=False):
    """A minimal workbook: row cells are (value, kind) with kind s|inline|n|date|date-custom|b."""
    shared = []
    sheet_rows = []
    for row_number, cells in enumerate(rows, 1):
        parts = []
        for column, (value, kind) in enumerate(cells):
            ref = f"{chr(65 + column)}{row_number}"
            if kind == "s":
                shared.append(value)
                parts.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            elif kind == "inline":
                parts.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
            elif kind == "b":
                parts.append(f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>')
            elif kind == "date":
                parts.append(f'<c r="{ref}" s="1"><v>{value}</v></c>')
            elif kind == "date-custom":
                parts.append(f'<c r="{ref}" s="2"><v>{value}</v></c>')
            else:
                parts.append(f'<c r="{ref}"><v>{value}</v></c>')
        sheet_rows.append(f'<row r="{row_number}">{"".join(parts)}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    properties = ' date1904="1"' if date1904 else ""
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>',
        "_rels/.rels": f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{main}" xmlns:r="{rel}">'
        f'<workbookPr{properties}/><sheets><sheet name="Firmen" sheetId="1" r:id="rId1"/>'
        f'<sheet name="Leer" sheetId="2" r:id="rId4"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/>'
        f'<Relationship Id="rId3" Type="{rel}/styles" Target="styles.xml"/>'
        f'<Relationship Id="rId4" Type="{rel}/worksheet" Target="worksheets/sheet2.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{main}" count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{text}</t></si>" for text in shared) + "</sst>",
        "xl/styles.xml": f'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{main}"><numFmts count="1">'
        '<numFmt numFmtId="164" formatCode="dd/mm/yyyy;@"/></numFmts>'
        '<cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{main}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>',
        "xl/worksheets/sheet2.xml": f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{main}"><sheetData/></worksheet>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def add_custom_fields(target):
    path = target / "schema/crm/datamodel.json"
    model = json.loads(path.read_text(encoding="utf-8"))
    fields = model["objects"]["company"]["fields"]
    fields["employees"] = {"type": "NUMBER", "label": "Mitarbeiter"}
    fields["isCustomer"] = {"type": "BOOLEAN", "label": "Bestandskunde"}
    fields["foundedOn"] = {"type": "DATE", "label": "Gründungsdatum"}
    fields["industry"] = {"type": "MULTI_SELECT", "label": "Branche", "options": [
        {"value": "SOFTWARE", "label": "Software", "color": "blue", "position": 0},
        {"value": "HANDEL", "label": "Handel", "color": "green", "position": 1},
        {"value": "INDUSTRIE", "label": "Industrie", "color": "red", "position": 2}]}
    fields["memo"] = {"type": "TEXT", "label": "Bemerkung"}
    path.write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# tests


def test_library():
    print("-- crm_tabular")
    check("decimal comma with grouping", crm_tabular.parse_decimal("1.234.567,89", "comma") == "1234567.89")
    check("decimal dot with grouping", crm_tabular.parse_decimal("1,234.56", "dot") == "1234.56")
    check("money with euro sign", crm_tabular.parse_money("1.234,56 €", "comma") == ("1234.56", "EUR"))
    check("money with leading code", crm_tabular.parse_money("EUR 1,234.56", "dot") == ("1234.56", "EUR"))
    check("decimal evidence", [crm_tabular.decimal_evidence(v) for v in ("1,5", "1.234", "1.234,5", "0,125")] == ["comma", None, "comma", "comma"])
    try:
        crm_tabular.parse_moment("01/02/2024")
        check("slash date without --date-format is ambiguous", False, "no error")
    except crm_tabular.ValueProblem as problem:
        check("slash date without --date-format is ambiguous", problem.rule == "ambiguous-date", problem.rule)
    check("slash date with DMY", crm_tabular.parse_moment("01/02/2024", "DMY").day.isoformat() == "2024-02-01")
    check("slash date with MDY", crm_tabular.parse_moment("01/02/2024", "MDY").day.isoformat() == "2024-01-02")
    check("German date D.M.YYYY", crm_tabular.parse_moment("1.4.1998").day.isoformat() == "1998-04-01")
    moment = crm_tabular.parse_moment("2024-03-15T10:30:00Z")
    check("ISO instant with Z on 3.9", crm_tabular.moment_to_instant(moment, "Europe/Berlin")[0] == "2024-03-15T10:30:00Z")
    local = crm_tabular.moment_to_instant(crm_tabular.parse_moment("15.03.2024 14:30"), "Europe/Berlin")
    check("local time converted with the wiki time zone", local == ("2024-03-15T13:30:00Z", "local-time"), str(local))
    check("Excel serial 1900 system", str(crm_tabular.excel_serial("45366", "date", False)) == "2024-03-15")
    check("Excel serial 1904 system", str(crm_tabular.excel_serial("45366", "date", True)) == "2028-03-16")
    check("Excel float noise rounded to 15 digits", str(crm_tabular.excel_number("2.0099999999999998")) == "2.01")
    check("booleans WAHR/FALSCH/ja/nein/x/1/0", [crm_tabular.parse_bool(v) for v in ("WAHR", "FALSCH", "ja", "nein", "x", "1", "0")] == [True, False, True, False, True, True, False])
    for data, name, needle in (
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "alt.xls", ".xls"),
        (b"%PDF-1.7 ...", "liste.pdf", "PDF"),
        (b"PK\x03\x04" + b"\x00" * 30, "kaputt.xlsx", "ZIP"),
    ):
        try:
            crm_tabular.parse_table(data, name)
            check(f"refuses {name}", False, "accepted")
        except crm_tabular.TabularError as exc:
            check(f"refuses {name}", needle in str(exc), str(exc))
    try:
        crm_tabular.parse_table(b'Name;Ort\nA;"offen\nB;Berlin\nC;Bonn\n', "offen.csv")
        check("unclosed quote refused instead of merging rows", False, "accepted")
    except crm_tabular.TabularError as exc:
        check("unclosed quote refused instead of merging rows", "Zeile 2" in str(exc), str(exc))
    try:
        crm_tabular.excel_serial("99999999", "date", False)
        check("serial beyond the calendar is a cell error", False, "accepted")
    except crm_tabular.ValueProblem as problem:
        check("serial beyond the calendar is a cell error", problem.rule == "excel-date", str(problem))
    graph = {"data": {"companies": {"edges": [{"node": {"__typename": "Company", "id": "x", "annualRevenue": {"amountMicros": 2010000, "currencyCode": "EUR"}}}]}}}
    table = crm_tabular.parse_table(json.dumps(graph).encode(), "c.json")
    check("GraphQL export form read", table.json_root == "companies" and table.rows[0].cells["annualRevenue"]["amountMicros"] == 2010000)


GERMAN_ROWS = [
    "Firmenname;Website;Straße;PLZ;Ort;Jahresumsatz;Mitarbeiter;Bestandskunde;Gründungsdatum;Branche;Bemerkung;Kundenbetreuer",
    'Acme GmbH;https://acme.example;Hauptstraße 1;10115;Berlin;1.234.567,89 €;1.250;WAHR;15.03.2024;Software, Handel;"Erste Zeile\r\nZweite Zeile";anna@firma.example',
    "Müller & Söhne KG;mueller-soehne.example;Marktplatz 3;01067;Dresden;2,01;12;FALSCH;1.4.1998;Handel;;",
    "CON;con.example;;;Köln;0;;nein;;Industrie;Reservierter Gerätename;",
    "",
]


def test_german_csv(session):
    print("-- German Excel CSV, cp1252 variant, mapping file")
    text = "\r\n".join(GERMAN_ROWS)
    utf8 = session.file("firmen-utf8.csv", b"\xef\xbb\xbf" + text.encode("utf-8"))
    mapping_path = session.work / "firmen-mapping.json"
    code, report, _ = session.run("crm_import.py", "inspect", "--file", utf8, "--object", "company", "--write-mapping", mapping_path)
    check("inspect exit 0", code == 0, report)
    check("inspect detects BOM, semicolon and rows", report.get("file", {}).get("encoding") == "utf-8-sig" and report["file"].get("delimiter") == ";" and report["file"].get("rows") == 3, report.get("file"))
    expected = {
        "Firmenname": "name", "Website": "domainName", "Straße": "address.addressStreet1", "PLZ": "address.addressPostcode",
        "Ort": "address.addressCity", "Jahresumsatz": "annualRevenue", "Mitarbeiter": "employees", "Bestandskunde": "isCustomer",
        "Gründungsdatum": "foundedOn", "Branche": "industry", "Bemerkung": "memo", "Kundenbetreuer": "accountOwner",
    }
    check("inspect proposes the full mapping", report.get("mapping") == expected, report.get("mapping"))
    check("inspect sees the multi-line cell", report["sample_rows"][0]["cells"]["Bemerkung"] == "Erste Zeile⏎Zweite Zeile", report["sample_rows"][0])
    check("inspect wrote the mapping file", mapping_path.is_file() and json.loads(mapping_path.read_text())["columns"]["Ort"] == "address.addressCity")
    code, report, raw, plan_path = session.plan(utf8, "company", match_key="domainName", mapping=mapping_path)
    check("plan German CSV", code == 0 and report.get("state") == "planned", raw[-1500:])
    check("plan counts 3 creations", report.get("counts", {}).get("create") == 3, report.get("counts"))
    rules = {(item["column"], item["rule"]) for item in report.get("normalizations", [])}
    for expected_rule in (("Jahresumsatz", "money"), ("Mitarbeiter", "decimal-comma"), ("Gründungsdatum", "date-dmy"), ("Bestandskunde", "boolean"), ("Branche", "select-label")):
        check(f"normalization reported {expected_rule}", expected_rule in rules, sorted(rules))
    check("decimal convention from column values", report.get("decimal_conventions", {}).get("Jahresumsatz", {}).get("convention") == "comma")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    events = [event for shard in plan["event_shards"] for event in shard["append"]]
    check("events carry origin kind, portable ref, sha256 and file line", all(event["origin"].get("kind") == "import" and event["origin"].get("ref") == "firmen-utf8.csv" and len(event["origin"].get("sha256", "")) == 64 for event in events) and sorted(event["origin"].get("row") for event in events) == [2, 4, 5], [event["origin"] for event in events])
    check("plan holds no local path", str(session.work) not in plan_path.read_text(encoding="utf-8"))
    code, result, _ = session.apply(plan_path, report)
    check("apply German CSV", code == 0 and result.get("state") == "applied", result)
    acme = session.find("company", name="Acme GmbH")
    check("Acme imported once", len(acme) == 1)
    if acme:
        data = acme[0].data
        check("amount exact in micros", data.get("annualRevenue.amountMicros") == 1234567890000 and data.get("annualRevenue.currencyCode") == "EUR", data)
        check("thousands dot in a number", data.get("employees") == 1250, data.get("employees"))
        check("WAHR becomes true", data.get("isCustomer") is True)
        check("DD.MM.YYYY becomes ISO", data.get("foundedOn") == "2024-03-15")
        check("multi-select by labels", data.get("industry") == ["SOFTWARE", "HANDEL"], data.get("industry"))
        check("multi-line cell kept", data.get("memo") == "Erste Zeile\nZweite Zeile", repr(data.get("memo")))
        check("account owner found by member e-mail", "records/workspace-member/" in str(data.get("accountOwner")), data.get("accountOwner"))
    mueller = session.find("company", name="Müller & Söhne KG")
    check("postcode with leading zero stays text", bool(mueller) and mueller[0].data.get("address.addressPostcode") == "01067")
    check("2,01 becomes exactly 2010000 micros", bool(mueller) and mueller[0].data.get("annualRevenue.amountMicros") == 2010000)
    check("D.M.YYYY", bool(mueller) and mueller[0].data.get("foundedOn") == "1998-04-01")
    con = session.find("company", name="CON")
    check("company CON stored under its UUID", len(con) == 1 and con[0].path == f"records/company/{con[0].id}.md" and (session.target / con[0].path).is_file())
    check("nein becomes false", bool(con) and con[0].data.get("isCustomer") is False)
    cp1252 = session.file("firmen-cp1252.csv", text.encode("cp1252"))
    code, report, raw, _ = session.plan(cp1252, "company", match_key="domainName", name="plan-cp1252.json")
    check("cp1252 variant read with a note", report.get("file", {}).get("encoding") == "cp1252" and any(note["rule"] == "encoding-fallback" for note in report.get("notes", [])), report.get("file"))
    check("cp1252 variant matches every row unchanged", report.get("counts", {}).get("unchanged") == 3 and report["counts"].get("create") == 0 and report["counts"].get("update") == 0, report.get("counts"))


def test_xlsx(session):
    print("-- XLSX with 1900 and 1904 date systems")
    header = [("Firmenname", "s"), ("Website", "s"), ("Gründungsdatum", "s"), ("Mitarbeiter", "s"), ("Bestandskunde", "s")]
    book1900 = xlsx_bytes([header, [("Beta AG", "inline"), ("beta.example", "s"), ("45366", "date"), ("250", "n"), (True, "b")],
                           [("Delta AG", "s"), ("delta.example", "s"), ("45366.75", "date-custom"), ("12.5", "n"), (False, "b")]])
    book1904 = xlsx_bytes([header, [("Gamma AG", "s"), ("gamma.example", "s"), ("45366", "date"), ("2.0099999999999998", "n"), (True, "b")]], date1904=True)
    path1900 = session.file("firmen-1900.xlsx", book1900)
    path1904 = session.file("firmen-1904.xlsx", book1904)
    code, report, _ = session.run("crm_import.py", "inspect", "--file", path1900, "--object", "company")
    check("inspect XLSX", code == 0 and report.get("file", {}).get("format") == "xlsx" and report["file"].get("date_system") == "1900" and report["file"].get("sheet") == "Firmen", report.get("file"))
    check("first sheet chosen with a note", any(note["rule"] == "first-sheet" for note in report.get("notes", [])))
    code, report, raw, result = session.import_file(path1900, "company", match_key="domainName", name="plan-1900.json")
    check("import XLSX (1900)", code == 0 and report.get("counts", {}).get("create") == 2, raw[-1500:])
    beta = session.find("company", name="Beta AG")
    check("serial 45366 in 1900 system is 2024-03-15", bool(beta) and beta[0].data.get("foundedOn") == "2024-03-15", beta and beta[0].data)
    check("inline string, number and boolean cells", bool(beta) and beta[0].data.get("employees") == 250 and beta[0].data.get("isCustomer") is True)
    delta = session.find("company", name="Delta AG")
    check("custom date format dd/mm/yyyy detected", bool(delta) and delta[0].data.get("foundedOn") == "2024-03-15", delta and delta[0].data)
    code, report, raw, result = session.import_file(path1904, "company", match_key="domainName", name="plan-1904.json")
    check("import XLSX (1904)", code == 0 and report.get("file", {}).get("date_system") == "1904", raw[-1500:])
    gamma = session.find("company", name="Gamma AG")
    check("serial 45366 in 1904 system is 2028-03-16", bool(gamma) and gamma[0].data.get("foundedOn") == "2028-03-16", gamma and gamma[0].data)
    check("Excel float noise does not reach the record", bool(gamma) and gamma[0].data.get("employees") == 2.01, gamma and gamma[0].data.get("employees"))
    xls = session.file("alt.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    code, report, _ = session.run("crm_import.py", "inspect", "--file", xls, "--object", "company")
    check("legacy .xls refused with a request for CSV or XLSX", code == 2 and ".xls" in report.get("error", "") and "CSV" in report.get("error", ""), report)


def test_people(session):
    print("-- people: two Max Müller, relation by domain, import order, duplicates")
    rows = [
        "Vorname;Nachname;E-Mail;Firma Domain;Position",
        "Max;Müller;max@mueller-soehne.example;mueller-soehne.example;Einkauf",
        "Max;Müller;max.mueller@andere.example;https://www.acme.example/;Vertrieb",
        "Erika;Muster;erika@unbekannt.example;unbekannt.example;CEO",
    ]
    path = session.file("personen.csv", "\r\n".join(rows) + "\r\n")
    code, report, raw, _ = session.plan(path, "person", match_key="emails.primaryEmail")
    check("plan with a missing company is not applicable", code == 1 and report.get("state") == "invalid" and report.get("applicable") is False, raw[-800:])
    check("mapping of person columns", report.get("mapping") == {"Vorname": "name.firstName", "Nachname": "name.lastName", "E-Mail": "emails", "Firma Domain": "company.domainName", "Position": "jobTitle"}, report.get("mapping"))
    errors = report.get("row_errors", [])
    check("relation error names row, column, rule and import order", any(error["row"] == 4 and error["column"] == "Firma Domain" and error["rule"] == "relation-not-found" and "Reihenfolge" in error["message"] for error in errors), errors)
    code, report, raw, result = session.import_file(path, "person", "--skip-invalid-rows", match_key="emails.primaryEmail", name="plan-people.json")
    check("--skip-invalid-rows plans the valid rows", code == 0 and report.get("counts", {}).get("create") == 2 and report["counts"].get("skipped") == 1, raw[-800:])
    check("skipped row listed", report.get("skipped_rows") == [{"line": 4, "rules": ["relation-not-found"]}], report.get("skipped_rows"))
    people = session.records("person")
    maxes = [record for record in people if record.data.get("crm_title") == "Max Müller"]
    check("two different Max Müller", len(maxes) == 2 and len({record.id for record in maxes}) == 2 and {record.data.get("emails.primaryEmail") for record in maxes} == {"max@mueller-soehne.example", "max.mueller@andere.example"})
    acme = session.find("company", name="Acme GmbH")[0]
    second = [record for record in maxes if record.data.get("emails.primaryEmail") == "max.mueller@andere.example"]
    check("company found through a normalized domain", bool(second) and acme.id in str(second[0].data.get("company")), second and second[0].data.get("company"))
    duplicate = session.file("dubletten.csv", "E-Mail;Vorname\nneu@example.org;A\nNEU@example.org;B\n")
    code, report, raw, _ = session.plan(duplicate, "person", match_key="emails.primaryEmail", name="plan-dup.json")
    rows_flagged = sorted(error["row"] for error in report.get("row_errors", []) if error["rule"] == "duplicate-in-file")
    check("duplicate match values inside the file are errors", code == 1 and rows_flagged == [2, 3], report.get("row_errors"))


def test_conflict_and_restore(session):
    print("-- conflicting match, soft delete and reactivation")
    first = session.find("person", **{"emails.primaryEmail": "max@mueller-soehne.example"})[0]
    second = session.find("person", **{"emails.primaryEmail": "max.mueller@andere.example"})[0]
    path = session.file("konflikt.csv", f"Id,E-Mail,Position\n{first.id},max.mueller@andere.example,Leitung\n")
    code, report, raw, _ = session.plan(path, "person", match_key="id", mode="update-only", name="plan-conflict.json")
    errors = report.get("row_errors", [])
    check("id and e-mail hitting two records is a row error", code == 1 and any(error["rule"] == "match-conflict" and error["column"] == "E-Mail" and second.id in error["message"] for error in errors), errors)
    report_delete, result_delete = session.transact([{"op": "delete", "object": "person", "record": second.id}], "delete")
    check("soft delete via crm_records", result_delete and result_delete.get("state") == "applied", report_delete)
    path = session.file("reaktivierung.csv", "E-Mail;Position\nmax.mueller@andere.example;Leitung\n")
    code, report, raw, result = session.import_file(path, "person", match_key="emails.primaryEmail", name="plan-restore.json")
    check("matching a deleted record restores it", code == 0 and report.get("counts", {}).get("restore") == 1 and report["counts"].get("update") == 1, raw[-800:])
    restored = session.find("person", **{"emails.primaryEmail": "max.mueller@andere.example"})
    check("restored record is active with the new value", bool(restored) and not restored[0].deleted and restored[0].data.get("jobTitle") == "Leitung", restored and restored[0].data)
    empty = session.file("leeren.csv", "E-Mail;Position\nmax.mueller@andere.example;\n")
    code, report, raw, result = session.import_file(empty, "person", match_key="emails.primaryEmail", name="plan-clear.json")
    cleared = session.find("person", **{"emails.primaryEmail": "max.mueller@andere.example"})
    check("empty cell clears the value", code == 0 and bool(cleared) and "jobTitle" not in cleared[0].data, cleared and cleared[0].data)
    check("missing column leaves values unchanged", bool(cleared) and cleared[0].data.get("name.firstName") == "Max" and cleared[0].data.get("company"))


def test_opportunities(session):
    print("-- opportunities: select by label, default stage, relation by domain")
    path = session.file("chancen.csv", "Name;Betrag;Phase;Abschlussdatum;Firma\nRelaunch;12.500,50 €;Angebot;31.12.2026;acme.example\nWartung;900;;;Müller & Söhne KG\nAusbau;EUR 1.000;Customer;2026-11-30;beta.example\n")
    code, report, raw, result = session.import_file(path, "opportunity", match_key="id", name="plan-opps.json")
    check("opportunities imported", code == 0 and report.get("counts", {}).get("create") == 3, raw[-1500:])
    relaunch = session.find("opportunity", name="Relaunch")
    check("German option label to API name", bool(relaunch) and relaunch[0].data.get("stage") == "PROPOSAL", relaunch and relaunch[0].data)
    check("date-only close date", bool(relaunch) and relaunch[0].data.get("closeDate") == "2026-12-31T00:00:00Z")
    check("amount with euro sign", bool(relaunch) and relaunch[0].data.get("amount.amountMicros") == 12500500000 and relaunch[0].data.get("amount.currencyCode") == "EUR")
    wartung = session.find("opportunity", name="Wartung")
    check("default stage NEW on creation", bool(wartung) and wartung[0].data.get("stage") == "NEW", wartung and wartung[0].data)
    check("relation by company name", bool(wartung) and "Müller" in str(wartung[0].data.get("company")))
    ausbau = session.find("opportunity", name="Ausbau")
    check("English option label", bool(ausbau) and ausbau[0].data.get("stage") == "CUSTOMER")
    if not (relaunch and wartung):
        return
    path = session.file("phase-leer.csv", f"Id;Phase\n{relaunch[0].id};\n{wartung[0].id};Termin\n")
    code, report, raw, _ = session.plan(path, "opportunities", match_key="id", mode="update-only", name="plan-required.json")
    check("clearing a required field is a row error from the core", code == 1 and any(error["row"] == 2 and error["rule"] == "core" and error["column"] == "Phase" for error in report.get("row_errors", [])), report.get("row_errors"))
    code, report, raw, result = session.import_file(path, "opportunities", "--skip-invalid-rows", match_key="id", mode="update-only", name="plan-required-skip.json")
    check("--skip-invalid-rows drops rows the core rejects and plans again", code == 0 and report.get("counts", {}).get("update") == 1 and report["counts"].get("skipped") == 1, raw[-800:])
    check("plural object name accepted", report.get("object") == "opportunities" and bool(session.find("opportunity", name="Wartung", stage="MEETING")))
    path = session.file("neu-anlegen.csv", "Firmenname;Website\nAcme Kopie;https://www.acme.example\n")
    code, report, raw, _ = session.plan(path, "company", match_key="domainName", mode="create", name="plan-create-exists.json")
    check("mode create refuses an existing match value", code == 1 and any(error["rule"] == "exists" for error in report.get("row_errors", [])), report.get("row_errors"))
    path = session.file("nur-update.csv", "Website;Firmenname\nunbekannt.example;X\n;Y\n")
    code, report, raw, _ = session.plan(path, "company", match_key="domainName", mode="update-only", name="plan-update-only.json")
    rules = sorted(error["rule"] for error in report.get("row_errors", []))
    check("update-only reports unknown and missing match values", code == 1 and rules == ["match-missing", "not-found"], rules)


def test_credentials(session):
    print("-- credential in a cell")
    secret = "ftp://kunde:Sommer2024x@files.example.com"
    path = session.file("geheim.csv", f"Firmenname;Website;Bemerkung\nEpsilon GmbH;epsilon.example;Zugang {secret}\n")
    code, report, raw, _ = session.plan(path, "company", match_key="domainName", name="plan-secret.json")
    check("credential is a row error", code == 1 and any(error["rule"] == "credential" and error["column"] == "Bemerkung" for error in report.get("row_errors", [])), raw[-800:])
    check("report never repeats the credential", "Sommer2024x" not in raw)
    code, _report, inspect_raw = session.run("crm_import.py", "inspect", "--file", path, "--object", "company")
    check("inspect masks the credential", code == 0 and "Sommer2024x" not in inspect_raw and "url-credentials" in inspect_raw)
    code, report, raw, plan_path = session.plan(path, "company", "--redact-credentials", match_key="domainName", name="plan-redact.json")
    text = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
    check("--redact-credentials replaces the value", code == 0 and "[credential removed]" in text and "Sommer2024x" not in text, raw[-800:])
    check("redaction reported", report.get("credentials") == [{"row": 2, "column": "Bemerkung", "kinds": ["url-credentials"], "action": "removed"}], report.get("credentials"))


def test_dates_ambiguous(session):
    print("-- ambiguous dates")
    path = session.file("datum.csv", "Firmenname;Website;Gründungsdatum\nZeta GmbH;zeta.example;01/02/2024\n")
    code, report, raw, _ = session.plan(path, "company", match_key="domainName", name="plan-ambiguous.json")
    check("01/02/2024 without --date-format is 'mehrdeutig'", code == 1 and any(error["rule"] == "ambiguous-date" and "mehrdeutig" in error["message"] for error in report.get("row_errors", [])), report.get("row_errors"))
    code, report, raw, plan_path = session.plan(path, "company", "--date-format", "DMY", match_key="domainName", name="plan-dmy.json")
    files = json.loads(plan_path.read_text(encoding="utf-8"))["files"] if plan_path.is_file() else []
    check("--date-format DMY", code == 0 and any('foundedOn: "2024-02-01"' in (entry["after"] or "") for entry in files), raw[-500:])


def test_header_forms(session):
    print("-- export header forms, labels and API names")
    company = session.file("kopf-firma.csv", "Name,Domain Name,Address / Address City,Address / Post Code,Annual Revenue / Amount,Annual Revenue / Currency,Account Owner Id\n")
    code, report, _ = session.run("crm_import.py", "inspect", "--file", company, "--object", "company")
    check("standard export headers for companies", report.get("mapping") == {
        "Name": "name", "Domain Name": "domainName", "Address / Address City": "address.addressCity", "Address / Post Code": "address.addressPostcode",
        "Annual Revenue / Amount": "annualRevenue.amount", "Annual Revenue / Currency": "annualRevenue.currencyCode", "Account Owner Id": "accountOwner.id",
    }, report.get("mapping"))
    company = session.file("kopf-firma-2.csv", "name,Domain / Domain URL,Address City,address.addressCountry,accountOwner.userEmail\n")
    code, report, _ = session.run("crm_import.py", "inspect", "--file", company, "--object", "company")
    check("documentation headers, subfield label and API paths", report.get("mapping") == {
        "name": "name", "Domain / Domain URL": "domainName.primaryLinkUrl", "Address City": "address.addressCity",
        "address.addressCountry": "address.addressCountry", "accountOwner.userEmail": "accountOwner.userEmail",
    }, report.get("mapping"))
    person = session.file("kopf-person.csv", "Name / First Name,Name / Last Name,Emails / Primary Email,Emails / Additional Emails,Company Id,companyDomain,Phones / Primary Phone Number\n")
    code, report, _ = session.run("crm_import.py", "inspect", "--file", person, "--object", "person")
    mapping = report.get("mapping", {})
    check("standard export headers for people", mapping.get("Name / First Name") == "name.firstName" and mapping.get("Emails / Primary Email") == "emails.primaryEmail"
          and mapping.get("Emails / Additional Emails") == "emails.additionalEmails" and mapping.get("Company Id") == "company.id"
          and mapping.get("Phones / Primary Phone Number") == "phones.primaryPhoneNumber", mapping)
    reasons = {item["column"]: item["reason"] for item in report.get("unmapped_columns", [])}
    check("second column for the same relation is left out", mapping.get("companyDomain") is None and "Company Id" in reasons.get("companyDomain", ""), reasons)
    check("match key candidates", report.get("match_key_candidates") == ["emails.primaryEmail"], report.get("match_key_candidates"))
    person = session.file("kopf-person-2.csv", "Vorname;Nachname;E-Mail;Weitere E-Mails;Telefon\n")
    code, report, _ = session.run("crm_import.py", "inspect", "--file", person, "--object", "person")
    check("whole e-mail column next to its subfield becomes the primary e-mail", report.get("mapping") == {
        "Vorname": "name.firstName", "Nachname": "name.lastName", "E-Mail": "emails.primaryEmail",
        "Weitere E-Mails": "emails.additionalEmails", "Telefon": "phones",
    }, report.get("mapping"))
    code, report, _ = session.run("crm_import.py", "inspect", "--file", person)
    check("object suggested without --object", (report.get("object_suggestions") or [{}])[0].get("object") == "person", report.get("object_suggestions"))


def test_kunden(session):
    print("-- kunden.csv: identical duplicate row, domain before company name, name parts, empty cells")
    rows = [
        "Firma;Domain;Ansprechpartner Vorname;Ansprechpartner Nachname;E-Mail;Telefon",
        "Acme GmbH;acme.example;Klara;Klein;klara@acme.example;+49 30 1111111",
        "Beta AG;beta.example;Bernd;Brot;bernd@beta.example;+49 30 2222222",
        "Gamma AG;gamma.example;Gabi;Grün;gabi@gamma.example;+49 30 3333333",
        "Delta AG;delta.example;Dieter;Dorn;dieter@delta.example;+49 30 4444444",
        "Müller & Söhne KG;mueller-soehne.example;Martha;Meier;martha@mueller-soehne.example;+49 30 5555555",
        "Acme GmbH;acme.example;Klara;Klein;klara@acme.example;+49 30 1111111",
    ]
    path = session.file("kunden.csv", "\r\n".join(rows) + "\r\n")
    code, report, _ = session.run("crm_import.py", "inspect", "--file", path, "--object", "person")
    expected = {
        "Firma": None, "Domain": "company.domainName", "Ansprechpartner Vorname": "name.firstName",
        "Ansprechpartner Nachname": "name.lastName", "E-Mail": "emails", "Telefon": "phones",
    }
    check("kunden.csv mapping: name parts with prefix, domain before company name", report.get("mapping") == expected, report.get("mapping"))
    reasons = {item["column"]: item["reason"] for item in report.get("unmapped_columns", [])}
    check("company name column left out with a reason", "Domain" in (reasons.get("Firma") or ""), reasons)
    code, report, raw, result = session.import_file(path, "person", match_key="emails.primaryEmail", name="plan-kunden.json")
    counts = report.get("counts", {})
    check("identical duplicate row does not block the import", code == 0 and counts.get("create") == 5 and counts.get("duplicates_ignored") == 1 and report.get("row_error_count") == 0, raw[-1200:])
    duplicates = report.get("duplicates_ignored", [])
    check("row 7 reported as duplicate-ignored of row 2", len(duplicates) == 1 and duplicates[0]["row"] == 7 and duplicates[0]["duplicate_of"] == 2 and duplicates[0]["rule"] == "duplicate-ignored", duplicates)
    klara = session.find("person", **{"emails.primaryEmail": "klara@acme.example"})
    acme = session.find("company", name="Acme GmbH")[0]
    check("Klara imported once and linked through the domain", len(klara) == 1 and acme.id in str(klara[0].data.get("company")), klara and klara[0].data)
    conflicting = session.file("kunden-widerspruch.csv", "E-Mail;Telefon\nklara@acme.example;+49 30 1111111\nklara@acme.example;+49 30 9999999\n")
    code, report, raw, _ = session.plan(conflicting, "person", match_key="emails.primaryEmail", name="plan-widerspruch.json")
    errors = [error for error in report.get("row_errors", []) if error["rule"] == "duplicate-in-file"]
    check("conflicting duplicates stay row errors and name the differing column", code == 1 and sorted(error["row"] for error in errors) == [2, 3] and all("Telefon" in error["message"] for error in errors), errors)
    empty = session.file("kunden-leer.csv", "E-Mail;Telefon\nbernd@beta.example;\n")
    code, report, raw, _ = session.plan(empty, "person", "--keep-empty-cells", match_key="emails.primaryEmail", name="plan-keep.json")
    cells = report.get("empty_cells", {})
    check("--keep-empty-cells keeps the existing phone", code == 0 and report.get("counts", {}).get("unchanged") == 1 and report.get("files") == 0 and cells.get("mode") == "keep" and cells.get("values") == 1, raw[-1200:])
    code, report, raw, plan_path = session.plan(empty, "person", match_key="emails.primaryEmail", name="plan-clear-phone.json")
    cells = report.get("empty_cells", {})
    columns = cells.get("columns") or [{}]
    check("report lists the value an empty cell would clear", code == 0 and cells.get("mode") == "clear" and cells.get("values") == 1 and columns[0].get("column") == "Telefon" and columns[0].get("count") == 1 and (columns[0].get("examples") or [None])[0] == {"line": 2, "record": "Bernd Brot", "field": "phones.primaryPhoneNumber"}, cells)
    revenue = session.file("umsatz-leer.csv", "Domain;Jahresumsatz\nacme.example;\n")
    revenue_mapping = session.file("umsatz-mapping.json", json.dumps({"format": "lmwiki-crm-import-mapping/1", "object": "company",
                                                                      "columns": {"Domain": "domainName", "Jahresumsatz": "annualRevenue"}}))
    revenue_code, revenue_report, _raw, revenue_plan = session.plan(revenue, "company", match_key="domainName", name="plan-umsatz-leer.json", mapping=revenue_mapping)
    revenue_cells = revenue_report.get("empty_cells", {})
    after = "".join(item.get("after", "") for item in json.loads(revenue_plan.read_text(encoding="utf-8")).get("files", []))
    check("empty amount cell clears only the amount and keeps the currency code", revenue_code == 0 and revenue_cells.get("values") == 1
          and revenue_cells["columns"][0]["examples"][0]["field"] == "annualRevenue.amount"
          and "annualRevenue.currencyCode:" in after and "annualRevenue.amountMicros:" not in after, (revenue_cells, after[-400:]))
    check("warning about clearing names the option", any("--keep-empty-cells" in warning for warning in report.get("warnings", [])), report.get("warnings"))
    code, result, _ = session.apply(plan_path, report)
    bernd = session.find("person", **{"emails.primaryEmail": "bernd@beta.example"})
    check("default import semantics clears the phone", code == 0 and bool(bernd) and not bernd[0].data.get("phones.primaryPhoneNumber"), bernd and bernd[0].data)


def test_multi_select_and_views(session):
    print("-- multi-select replaces, views, split files, deleted records")
    path = session.file("branche.csv", "Website;Branche\nacme.example;Industrie\n")
    code, report, raw, result = session.import_file(path, "company", match_key="domainName", mode="update-only", name="plan-multi.json")
    acme = session.find("company", name="Acme GmbH")
    check("multi-select replaces instead of adding", code == 0 and bool(acme) and acme[0].data.get("industry") == ["INDUSTRIE"], acme and acme[0].data.get("industry"))
    views_path = session.target / "schema/crm/views.json"
    views = json.loads(views_path.read_text(encoding="utf-8"))
    views["views"].append({
        "id": "kunden-export", "object": "company", "type": "table", "label": "Kunden", "fields": ["name", "annualRevenue", "isCustomer"],
        "sort": [{"field": "name", "direction": "desc"}],
        "filter": {"op": "AND", "conditions": [{"field": "isCustomer", "operand": "IS", "value": True}]},
    })
    views_path.write_text(json.dumps(views, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out = session.work / "ansicht-kunden-export.csv"
    code, report, _ = session.run("crm_export.py", "csv", "--object", "company", "--view", "kunden-export", "--output", out)
    lines = out.read_text(encoding="utf-8").splitlines() if out.is_file() else []
    names = [line.split(",")[1] for line in lines[1:]]
    check("view filter and sort applied", code == 0 and names == ["Gamma AG", "Beta AG", "Acme GmbH"], names)
    check("view fields only", bool(lines) and lines[0] == "Id,Name,Annual Revenue / Amount,Annual Revenue / Currency,Bestandskunde", lines[:1])
    check("booleans exported as TRUE", all(line.endswith(",TRUE") for line in lines[1:]), lines[1:])
    out = session.work / "split.csv"
    code, report, _ = session.run("crm_export.py", "csv", "--object", "company", "--max-rows", "3", "--output", out)
    check("export split into numbered files", code == 0 and [item["file"] for item in report.get("files", [])] == ["split-1.csv", "split-2.csv"] and all(item["rows"] == 3 for item in report["files"]), report.get("files"))
    con = session.find("company", name="CON")[0]
    session.transact([{"op": "delete", "object": "company", "record": con.id}], "delete-con")
    out = session.work / "ohne-geloeschte.csv"
    session.run("crm_export.py", "csv", "--object", "company", "--output", out)
    check("deleted records left out by default", con.id not in out.read_text(encoding="utf-8"))
    out = session.work / "mit-geloeschten.csv"
    code, report, _ = session.run("crm_export.py", "csv", "--object", "company", "--include-deleted", "--output", out)
    text = out.read_text(encoding="utf-8")
    check("--include-deleted adds the rows and a Deleted at column", code == 0 and con.id in text and "Deleted at" in text.splitlines()[0], report)


def test_round_trip(session):
    print("-- round trip export -> import without changes")
    formula = session.file("formel.csv", "Website;Bemerkung\ncon.example;=SUMME(A1:A3)\n")
    code, report, raw, result = session.import_file(formula, "company", match_key="domainName", mode="update-only", name="plan-formula.json")
    check("formula-like text stored as text", code == 0 and session.find("company", name="CON")[0].data.get("memo") == "=SUMME(A1:A3)", raw[-500:])
    for object_name in ("company", "person", "opportunity"):
        out = session.work / f"export-{object_name}.csv"
        code, report, _ = session.run("crm_export.py", "csv", "--object", object_name, "--output", out)
        check(f"export {object_name} as standard CSV", code == 0 and report.get("rows", 0) > 0, report)
        header = out.read_text(encoding="utf-8").splitlines()[0]
        if object_name == "company":
            check("standard column forms", header.startswith("Id,Name,Domain Name / Link URL") and "Address / City" in header and "Annual Revenue / Amount" in header and "Account Owner Id" in header, header)
            check("formula guarded with a zero-width joiner as is usual in CRMs", "‍=SUMME(A1:A3)" in out.read_text(encoding="utf-8"))
        if object_name == "person":
            check("relation id on the many side", "Company Id" in header and "Name / First Name" in header and "Emails / Primary Email" in header, header)
        code, plan_report, raw, _ = session.plan(out, object_name, match_key="id", name=f"plan-rt-{object_name}.json")
        counts = plan_report.get("counts", {})
        check(f"round trip {object_name}: nothing changes", code == 0 and counts.get("create") == 0 and counts.get("update") == 0 and counts.get("unchanged") == report.get("rows") and plan_report.get("files") == 0, raw[-1500:])
    out = session.work / "export-company.json"
    code, report, _ = session.run("crm_export.py", "json", "--object", "company", "--output", out)
    items = json.loads(out.read_text(encoding="utf-8"))
    check("JSON export in API form", code == 0 and isinstance(items, list) and isinstance(items[0].get("annualRevenue"), dict) and "accountOwnerId" in items[0], items[:1])
    code, plan_report, raw, _ = session.plan(out, "company", match_key="id", name="plan-rt-json.json")
    check("round trip JSON: nothing changes", code == 0 and plan_report.get("counts", {}).get("unchanged") == len(items) and plan_report.get("files") == 0, raw[-1500:])
    wrapped = session.file("graphql.json", json.dumps({"data": {"companies": {"edges": [{"node": {"__typename": "Company", **item}} for item in items]}}}, ensure_ascii=False))
    code, plan_report, raw, _ = session.plan(wrapped, "company", match_key="id", name="plan-rt-graphql.json")
    check("round trip GraphQL form: nothing changes", code == 0 and plan_report.get("counts", {}).get("unchanged") == len(items), raw[-1500:])
    plain = session.work / "export-company-plain.csv"
    code, report, _ = session.run("crm_export.py", "csv", "--object", "company", "--format", "plain", "--output", plain)
    check("plain export uses BOM, semicolon and decimal comma", code == 0 and plain.read_bytes().startswith(b"\xef\xbb\xbf") and report.get("delimiter") == ";" and "1234567,89" in plain.read_text(encoding="utf-8-sig"), report)
    code, plan_report, raw, _ = session.plan(plain, "company", match_key="id", name="plan-rt-plain.json")
    check("round trip plain CSV: nothing changes", code == 0 and plan_report.get("counts", {}).get("update") == 0 and plan_report.get("counts", {}).get("create") == 0, raw[-1500:])
    people_json = [
        {"name": {"firstName": "Jana", "lastName": "API"}, "emails": {"primaryEmail": "jana@api.example", "additionalEmails": ["j@api.example"]},
         "company": {"id": session.find("company", name="Acme GmbH")[0].id, "name": "egal"}, "jobTitle": "CTO", "createdAt": "2024-01-02T03:04:05.000Z"},
    ]
    api = session.file("people-api.json", json.dumps({"data": {"people": {"edges": [{"node": item} for item in people_json]}}}))
    code, plan_report, raw, result = session.import_file(api, "person", match_key="emails.primaryEmail", name="plan-api.json")
    jana = session.find("person", **{"emails.primaryEmail": "jana@api.example"})
    check("API JSON with nested composites and relation", code == 0 and bool(jana) and jana[0].data.get("name.lastName") == "API" and jana[0].data.get("emails.additionalEmails") == ["j@api.example"] and "Acme" in str(jana[0].data.get("company")), raw[-1500:])
    check("createdAt kept on creation", bool(jana) and jana[0].data.get("crm_created_at") == "2024-01-02T03:04:05Z", jana and jana[0].data.get("crm_created_at"))


def test_junctions(session):
    print("-- noteTargets junction import and export")
    report, result = session.transact([{"op": "create", "object": "note", "ref": "n1", "values": {"title": "Erstgespräch", "bodyV2": "Interesse an Relaunch"}}], "note")
    check("note created", result and result.get("state") == "applied", report)
    note = session.find("note", title="Erstgespräch")[0]
    max_one = session.find("person", **{"emails.primaryEmail": "max@mueller-soehne.example"})[0]
    acme = session.find("company", name="Acme GmbH")[0]
    path = session.file("noteTargets.csv", f"id,noteId,targetPersonId,targetCompanyId,targetOpportunityId\n,{note.id},{max_one.id},,\n,{note.id},,{acme.id},\n")
    code, report, raw, result = session.import_file(path, "noteTarget", match_key="id", name="plan-junction.json")
    check("junction file adds links", code == 0 and report.get("counts", {}).get("links_added") == 2 and report.get("junction") is True, raw[-1500:])
    targets = session.find("note", title="Erstgespräch")[0].data.get("targets") or []
    check("note targets hold person and company", len(targets) == 2 and any(max_one.id in item for item in targets) and any(acme.id in item for item in targets), targets)
    out = session.work / "noteTargets-export.csv"
    code, report, _ = session.run("crm_export.py", "junctions", "--object", "note", "--output", out)
    lines = out.read_text(encoding="utf-8").splitlines()
    check("junction export", code == 0 and lines[0] == "id,noteId,targetPersonId,targetCompanyId,targetOpportunityId" and len(lines) == 3, lines)
    code, report, raw, _ = session.plan(out, "noteTarget", match_key="id", name="plan-junction-rt.json")
    check("junction round trip adds nothing", code == 0 and report.get("counts", {}).get("links_added") == 0 and report.get("files") == 0, raw[-1000:])
    sample = session.work / "sample-company.csv"
    code, report, _ = session.run("crm_export.py", "sample", "--object", "company", "--output", sample)
    sample_lines = sample.read_text(encoding="utf-8").splitlines()
    check("sample file with columns and one example row", code == 0 and len(sample_lines) == 2 and "Domain Name / Link URL" in sample_lines[0] and "Id" not in sample_lines[0].split(","), sample_lines[:1])


def test_lint(session):
    code_view = session.wiki.locked("lint_wiki.py", "--target", session.target, "--check-only", expect=(0, 1))
    errors = [error for error in code_view.get("errors", []) if "records/" in error or "crm-events" in error]
    check("lint finds no record or event errors after the imports", not errors, errors[:5])


def test_performance(rows):
    print(f"-- performance: plan and apply {rows} people")
    session = Session("perf")
    try:
        companies = ["Firmenname;Website"] + [f"Firma {index};firma{index}.example" for index in range(10)]
        code, report, raw, result = session.import_file(session.file("perf-firmen.csv", "\n".join(companies) + "\n"), "company", match_key="domainName", name="plan-perf-companies.json")
        check("perf companies", code == 0, raw[-500:])
        lines = ["Vorname;Nachname;E-Mail;Firma Domain;Position;Telefon"]
        lines += [f"Vor{index};Nach{index};person{index}@perf.example;firma{index % 10}.example;Einkauf;+49 30 {index:07d}" for index in range(rows)]
        path = session.file("perf-personen.csv", "\n".join(lines) + "\n")
        started = time.time()
        code, report, raw, plan_path = session.plan(path, "person", match_key="emails.primaryEmail", name="plan-perf.json")
        planned = time.time() - started
        check(f"perf plan {rows} rows", code == 0 and report.get("counts", {}).get("create") == rows, raw[-800:])
        started = time.time()
        apply_code, result, _ = session.apply(plan_path, report)
        applied = time.time() - started
        check(f"perf apply {rows} rows", apply_code == 0 and result.get("records_written") == rows, result)
        files = len(list((session.target / "records/person").glob("*.md")))
        check("perf record files written", files == rows, files)
        MEASUREMENTS.update({"rows": rows, "plan_seconds": round(planned, 1), "apply_seconds": round(applied, 1), "plan_file_mb": round(plan_path.stat().st_size / 1e6, 1)})
        print(f"      plan {planned:.1f} s, apply {applied:.1f} s, plan file {plan_path.stat().st_size / 1e6:.1f} MB")
    finally:
        session.close()


def main():
    skip_performance = "--skip-performance" in sys.argv
    rows = int(sys.argv[sys.argv.index("--rows") + 1]) if "--rows" in sys.argv else 5000
    WORK.mkdir(parents=True, exist_ok=True)
    test_library()
    session = Session("main")
    try:
        add_custom_fields(session.target)
        report, result = session.transact([{"op": "create", "object": "workspaceMember", "values": {"name": {"firstName": "Anna", "lastName": "Admin"}, "userEmail": "anna@firma.example"}}], "member")
        check("workspace member created", result and result.get("state") == "applied", report)
        test_german_csv(session)
        test_xlsx(session)
        test_people(session)
        test_conflict_and_restore(session)
        test_opportunities(session)
        test_credentials(session)
        test_dates_ambiguous(session)
        test_header_forms(session)
        test_round_trip(session)
        test_junctions(session)
        test_kunden(session)
        test_multi_select_and_views(session)
        test_lint(session)
    finally:
        session.close()
    if not skip_performance:
        test_performance(rows)
    if MEASUREMENTS:
        print("measurements:", json.dumps(MEASUREMENTS))
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES")
        for failure in FAILURES:
            print(" -", failure[:500])
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
