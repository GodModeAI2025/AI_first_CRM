#!/usr/bin/env python3
"""Run the local regression suites, stopping on any failure."""
import os
from pathlib import Path
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[1]
for suite in sorted((ROOT / 'qa/tests').glob('*/test_*.py')):
    print(f'Running {suite.relative_to(ROOT)}', flush=True)
    result = subprocess.run([sys.executable, str(suite)], cwd=ROOT, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    if result.returncode:
        sys.exit(result.returncode)
print('All regression suites passed.')
