#!/usr/bin/env python3
"""Build the reproducible, installable product package."""
import hashlib
from pathlib import Path
import zipfile
ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'ai-first-crm'
OUT = ROOT / 'dist'
OUT.mkdir(exist_ok=True)
package = OUT / 'ai-first-crm.skill'
with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(SKILL.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc' or path.name == '.DS_Store':
            continue
        info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), (2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, path.read_bytes())
checksum = hashlib.sha256(package.read_bytes()).hexdigest()
(package.with_suffix('.skill.sha256')).write_text(f'{checksum}  {package.name}\n', encoding='utf-8')
print(f'{package.name}: {checksum}')
