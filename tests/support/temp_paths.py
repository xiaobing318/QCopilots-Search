"""Resolve Python fixture roots without treating an empty value as the cwd."""
from __future__ import annotations

import os
from pathlib import Path


def temporary_root() -> Path | None:
    requested = os.environ.get("QCOPILOTS_SEARCH_TEST_ROOT")
    if not requested:
        return None  # tempfile chooses the platform's standard temporary root.
    root = Path(requested).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise NotADirectoryError(f"Test root is not a directory: {root}")
    return root
