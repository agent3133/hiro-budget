"""View settings remembered between sessions (open tab, collapsed groups, ...).

Stored as JSON next to the database. Losing the file only resets the views, so read
and write errors are logged and otherwise ignored.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class UiState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._data = data
        except (OSError, ValueError) as e:
            log.warning("cannot read view settings %s (%s); using defaults", path, e)

    def get(self, key: str, default: Any = None) -> Any:
        value = self._data.get(key, default)
        # a hand-edited or outdated file must not break the views
        if default is not None and not isinstance(value, type(default)):
            return default
        return value

    def set(self, key: str, value: Any) -> None:
        if self._data.get(key) == value:
            return
        self._data[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("cannot save view settings %s (%s)", self.path, e)
