"""Make ``scripts/release`` and ``packaging/macos`` importable: their helpers run as plain
scripts in the workflows."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELEASE_SCRIPTS = ROOT / "scripts" / "release"
MACOS_PACKAGING = ROOT / "packaging" / "macos"
for path in (RELEASE_SCRIPTS, MACOS_PACKAGING):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
