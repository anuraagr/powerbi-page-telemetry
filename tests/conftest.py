"""Shared pytest fixtures and path setup."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ETL_DIR = REPO_ROOT / "etl"

if str(ETL_DIR) not in sys.path:
    sys.path.insert(0, str(ETL_DIR))
