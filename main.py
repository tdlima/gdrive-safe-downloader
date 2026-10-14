"""Ponto de entrada: ``python main.py --help``."""

import sys

if sys.version_info < (3, 11):
    sys.exit("Este programa requer Python 3.11 ou superior.")

from gdsd.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
