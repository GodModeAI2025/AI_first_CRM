# AI First CRM

**AI First ab Tag 0. Kundenarbeit beginnt mit deinem Anliegen.**

Ein CRM, dessen Bedienung von Anfang an als Gespräch gedacht ist. Du beschreibst, was du erreichen möchtest; dein Agent strukturiert Kundenwissen, Kontakte, Verkaufschancen und nächste Schritte. Daten bleiben in einem portablen Markdown-Arbeitsraum, Änderungen werden vorab gezeigt und bestätigt.

[Projektseite](https://godmodeai2025.github.io/AI_first_CRM/) · [Installationspaket](dist/ai-first-crm.skill) · [16 CRM-Beispiele](https://godmodeai2025.github.io/AI_first_CRM/#ablaufe) · [Funktionsumfang](ai-first-crm/references/crm-coverage.md) · [Apache 2.0](LICENSE)

## Was du bekommst

Ein vollständiger lokal ausführbarer Agent-Skill mit Python-Laufzeit, Betriebsregeln und HTML-Ansichten. Kein gehosteter CRM-Dienst: Ein Agent mit lokalem Dateizugriff führt die mitgelieferten Werkzeuge aus. Die Python-Laufzeit selbst benötigt keine externen Bibliotheken und versendet keine Daten. Die Datenverarbeitung deines gewählten Agenten hängt von dessen Anbieter und Einstellungen ab.

- Firmen, Personen, Verkaufschancen, Aufgaben, Notizen, eigene Objekte und Felder.
- CSV-, XLSX- und JSON-Import mit Vorschau, Dublettenerkennung und Export.
- Pipelines, Tabellen, Kanban, Kalender und Dashboards als lokale HTML-Ansichten.
- Workflows, importierte Mails und Termine, Anhänge und Kampagnenentwürfe.
- Versionierte Wissensseiten mit Belegen, Änderungsprotokoll und Rückgängig-Funktionen.
- Löschvorschauen und Werkzeuge zur Entfernung personenbezogener Daten innerhalb des Arbeitsraums.

## In wenigen Schritten starten

Du brauchst **Python 3.9+** und einen Agenten, der Skills laden und lokale Dateien bearbeiten kann.

1. Lade [das Paket](dist/ai-first-crm.skill) und [die Prüfsumme](dist/ai-first-crm.skill.sha256) aus demselben Stand herunter. Im Downloadordner prüfst du es mit `shasum -a 256 -c ai-first-crm.skill.sha256`.
2. Installiere den enthaltenen Ordner `ai-first-crm` im Skill-Verzeichnis deines Agenten. Für Claude Code kannst du ihn nach `~/.claude/skills/`, für Codex nach `~/.codex/skills/` entpacken. Starte danach eine neue Sitzung. Alternativ kopierst du den Ordner aus diesem Repository.
3. Sag deinem Agenten: **„Nutze ai-first-crm. Lege unter [dein Ordner] einen CRM-Arbeitsraum für unser Vertriebsteam an. Sprache Deutsch, Währung EUR. Zeig mir den Vorschlag vor dem Anlegen.“**

Der Agent klärt Zweck und Regeln, legt nach Bestätigung den Wissensraum an und richtet die CRM-Schicht ein. Gib nur Ordner und Dateien frei, die dafür vorgesehen sind.

## Dein Anliegen wird zum Arbeitsablauf

| Du sagst | Das CRM unterstützt dich mit |
|---|---|
| „Importiere diese Kundenliste. Zeig mir Änderungen und Dubletten.“ | einer Vorschau vor der Übernahme |
| „Wie sieht unsere gewichtete Pipeline aus?“ | Summen je Phase und Währung |
| „Wenn ein Deal gewonnen ist, bereite das Onboarding vor.“ | Aufgaben und Nachrichtenentwürfen |
| „Ordne diese exportierten Mails den Kontakten zu.“ | verknüpften Nachrichten und Schwärzung erkannter Zugangsdaten |
| „Mach die letzte Übernahme rückgängig.“ | einem Revert der Transaktion |

## Du behältst die Kontrolle

**Planen → Vorschau → Bestätigung → Anwenden.** Pläne sind an Prüfsummen gebunden. Ein zwischenzeitlich veränderter Stand erfordert eine neue Vorschau. Eine Wartungssperre koordiniert Schreibzugriffe; veröffentlichte Stände werden über Dateiprüfsummen geprüft.

Dein Arbeitsraum enthält `records/` für CRM-Daten, `wiki/` für Wissen, `schema/` für Regeln, `meta/` für Verlauf und `graph/` für Browseransichten. Die HTML-Ansichten sind offline lesbar und schreibgeschützt.

## Grenzen, die du kennen solltest

- Bedienung erfolgt über das Gespräch; die HTML-Ansichten sind keine Bearbeitungsoberfläche.
- Es gibt keinen Hintergrunddienst. Geplante Automatisierungen werden beim nächsten Aufruf verarbeitet.
- Mails, Einladungen und HTTP-Anfragen werden als Entwürfe oder Anfragedateien vorbereitet und nicht versendet.
- Mail- und Kalenderdaten werden aus Dateien importiert, nicht aus synchronisierten Postfächern.
- Rollen koordinieren die Werkzeugnutzung. Dateiberechtigungen und Zugriffsschutz musst du in der Ablage einrichten.
- Löschwerkzeuge erreichen keine externen Backups, alten Exporte oder bereits versendeten Nachrichten. Sie ersetzen keine Datenschutzprüfung.
- Binäre Anhänge werden nicht inhaltlich auf Zugangsdaten geprüft. Dateien über 25 MB bleiben außerhalb des Arbeitsraums.

Die [Betriebsdokumentation](ai-first-crm/SKILL.md) und der [Funktionsumfang](ai-first-crm/references/crm-coverage.md) beschreiben die Details.

## Entwicklung

| Verzeichnis | Inhalt |
|---|---|
| `ai-first-crm/` | Skill, Laufzeit, Betriebsdokumentation und Vorlagen |
| `dist/` | installierbares Paket und SHA-256-Prüfsumme |
| `docs/` | Landingpage |
| `qa/` | lokale Regressionstests mit synthetischen Daten |
| `tools/` | Paketbau und Testaufruf |

Tests: `python3 tools/check.py`. Paket: `python3 tools/package.py`. Beides benötigt nur Python und funktioniert ohne Netzwerk. Die Tests erzeugen ausschließlich Testarbeitsräume unter `qa/test-wikis/`.

## Lizenz

Copyright 2026 GodModeAI2025. Code, Dokumentation und Vorlagen dieses Projekts stehen unter der **Apache License, Version 2.0**. Siehe [LICENSE](LICENSE) und [NOTICE](NOTICE). Der eingebettete Diagramm-Viewer und dessen Schrift behalten ihre MIT- bzw. OFL-Lizenz; siehe [Drittanbieterhinweise](THIRD_PARTY_NOTICES.md). Beiträge werden unter derselben Lizenz eingebracht.
