"""PyInstaller entry point for Sombra.app: the same ``sombra`` CLI the wheel installs."""

import sys

from sombra.cli import main

if __name__ == "__main__":
    sys.exit(main())
