"""The log file of the GUI (#79): warnings and errors with their tracebacks, so a problem
can be reported even when the app was started without a console (the exe from Explorer).

``<data folder>/logs/hiro-budget.log``, rotated at 1 MB, three old files kept. The data
folder is private already (it holds the database); bank texts are not logged at WARNING.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from hiro_budget.config import Settings

LOG_FOLDER = "logs"
LOG_FILE = "hiro-budget.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

log = logging.getLogger(__name__)
_handler: RotatingFileHandler | None = None


def log_dir(settings: Settings) -> Path:
    return settings.data_dir / LOG_FOLDER


def log_path() -> Path | None:
    """The file written to, None before ``open_log_file`` or when it failed."""
    return Path(_handler.baseFilename) if _handler is not None else None


def open_log_file(settings: Settings) -> Path | None:
    """Add the rotating file handler to the root logger (once; again for another folder).
    A folder that cannot be written is only reported: the app runs without a log file."""
    global _handler
    folder = log_dir(settings)
    if _handler is not None:
        if Path(_handler.baseFilename).parent == folder.resolve():
            return log_path()
        close_log_file()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            folder / LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as e:
        log.warning("no log file in %s: %s", folder, e)
        return None
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(FORMAT))
    logging.getLogger().addHandler(handler)
    _handler = handler
    return log_path()


def close_log_file() -> None:
    """Release the file (before a restart on a moved data folder, in tests)."""
    global _handler
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        _handler.close()
        _handler = None
