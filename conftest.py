"""Make `pytest` work from this directory with no arguments.

`tests/` contains an `__init__.py`, so pytest inserts the PARENT directory on
sys.path rather than `tests/` itself. The test modules do a flat
`import helpers`, and the pipeline modules import each other flatly too
(`import detect`, not `from . import detect`) because they are invoked as
`python3 scripts/finance_pipeline/run.py`, which puts that directory on the path.

Without this file, a bare `pytest` fails with 16 collection errors and the suite
looks broken when it is not. Added 2026-08-25 after the suite was found
unrunnable without an undocumented PYTHONPATH.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent
for _p in (_ROOT / "tests", _ROOT / "scripts" / "finance_pipeline", _ROOT / "scripts"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Optional machine settings (2026-09-24). A file named finance.local.env beside
# this conftest may set FINANCE_LIVE_MODEL (the real workbook) and
# FINANCE_REAL_DATA_DIR (the folder holding real exports). It is never
# committed to a public tree. Without it, every real-data test skips and the
# suite runs on synthetic data only. Values already in the environment win.
_LOCAL_ENV = _ROOT / "finance.local.env"
if _LOCAL_ENV.is_file():
    import os

    for _line in _LOCAL_ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _value = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))
