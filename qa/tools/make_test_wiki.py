#!/usr/bin/env python3
"""Create a released, initialized test wiki with placeholder identity values.

Usage: make_test_wiki.py <skill-dir> <target-dir> [--language de]
Prints the JSON result of initialize_wiki.py. Only for local testing.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    skill = Path(sys.argv[1]).resolve()
    target = Path(sys.argv[2]).resolve()
    language = sys.argv[sys.argv.index("--language") + 1] if "--language" in sys.argv else "de"
    scripts = skill / "scripts"
    identity = {
        "identity": {
            "purpose": "Testwiki fuer CRM-Funktionen",
            "knowledge_types": ["Firmen", "Personen", "Verkaufschancen"],
            "audience": "Vertriebsteam",
            "answer_language": language,
            "form_of_address": "du",
            "tone": "sachlich",
            "detail": "knapp",
            "answer_structure": "Antwort zuerst",
            "citation_display": "Quellen-IDs",
            "uncertainty_style": "offen benennen",
            "history_presentation": "aktueller Stand zuerst",
            "boundaries": "nur belegte Inhalte",
            "taboos": "keine Vermutungen als Fakten",
        },
        "content_policy": {
            "update_model": "hybrid",
            "supersession_policy": "explizit verknuepfen",
            "removal_policy": "preview-confirm-never-automatic",
            "conflict_policy": "preserve-and-disclose",
        },
    }
    with tempfile.TemporaryDirectory(dir=str(target.parent)) as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
        plan_out = tmp_path / "plan.json"
        subprocess.run([sys.executable, str(scripts / "plan_identity.py"), "--input", str(tmp_path / "identity.json"),
                        "--wiki-language", language, "--output", str(plan_out)], check=True, capture_output=True)
        plan = json.loads(plan_out.read_text(encoding="utf-8"))
        completed = subprocess.run([
            sys.executable, str(scripts / "initialize_wiki.py"), "--target", str(target),
            "--title", "CRM Testwiki", "--topic", "Kundenbeziehungen im Test",
            "--wiki-language", language, "--wiki-language-label", "Deutsch" if language == "de" else "English",
            "--identity-plan", str(plan_out), "--expect-identity-sha256", plan["proposal_sha256"],
            "--owner", "test-harness",
        ], capture_output=True, text=True)
        print(completed.stdout or completed.stderr)
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
