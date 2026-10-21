from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
REPLACE_ATTEMPTS = 5


class HealthReporter:
    """Write a small atomic JSON status file for local/Azure watchdogs."""

    def __init__(self, path: Path, **initial: Any) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = dict(initial)
        self.update(state="starting")

    def update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)
            self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            # A reader without FILE_SHARE_DELETE (PowerShell Get-Content in the
            # watchdog) makes the replace fail on Windows. A skipped heartbeat
            # is harmless; crashing the bot over it forces a live flatten.
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, self.path)
                    return
                except PermissionError:
                    if attempt + 1 < REPLACE_ATTEMPTS:
                        time.sleep(0.05 * (attempt + 1))
            LOGGER.warning("health file %s locked by a reader; skipped this update", self.path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)
