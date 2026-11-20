"""Runtime configuration, driven by environment variables."""
from __future__ import annotations

import os

# SQLite database file location. Overridable for tests / containers.
DB_PATH = os.environ.get("EXHIBITION_DB", "/data/exhibition.db")

# SQLite busy timeout in milliseconds: writers retry on SQLITE_BUSY
# instead of failing when another transaction holds the write lock.
BUSY_TIMEOUT_MS = int(os.environ.get("EXHIBITION_BUSY_TIMEOUT_MS", "5000"))
