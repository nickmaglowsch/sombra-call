"""Make ``scripts/release`` importable: its helpers run as plain scripts in the workflow."""

import sys
from pathlib import Path

RELEASE_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "release"
if str(RELEASE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(RELEASE_SCRIPTS))
