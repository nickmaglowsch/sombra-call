"""Make ``packaging/homebrew`` importable: its renderer runs as a plain script in CI."""

import sys
from pathlib import Path

HOMEBREW = Path(__file__).resolve().parents[2] / "packaging" / "homebrew"
if str(HOMEBREW) not in sys.path:
    sys.path.insert(0, str(HOMEBREW))
