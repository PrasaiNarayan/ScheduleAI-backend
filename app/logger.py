"""
Centralised logging setup for ScheduleAI.
Creates two log files:
  logs/app.log    — all INFO+ messages (rotating, 5MB × 3 backups)
  logs/errors.log — ERROR+ only        (rotating, 5MB × 5 backups)

Usage anywhere in the app:
    from app.logger import get_logger
    log = get_logger(__name__)
    log.info("Solving workforce/shift with 3 employees")
    log.error("CP-SAT crashed", exc_info=True)
"""
from __future__ import annotations
import logging
import logging.handlers
import os
from pathlib import Path

# ── Log directory (project root / logs/) ─────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

APP_LOG   = LOG_DIR / "app.log"
ERROR_LOG = LOG_DIR / "errors.log"

# ── Formatters ────────────────────────────────────────────────────────────────
DETAILED = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
SIMPLE = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── Root logger setup (called once at import time) ────────────────────────────
def _setup_root() -> None:
    root = logging.getLogger()
    if root.handlers:          # already configured — skip
        return
    root.setLevel(logging.DEBUG)

    # 1. Console — INFO+ with simple format
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(SIMPLE)
    root.addHandler(console)

    # 2. app.log — INFO+ rotating file
    app_handler = logging.handlers.RotatingFileHandler(
        APP_LOG, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(DETAILED)
    root.addHandler(app_handler)

    # 3. errors.log — ERROR+ rotating file
    err_handler = logging.handlers.RotatingFileHandler(
        ERROR_LOG, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(DETAILED)
    root.addHandler(err_handler)

    root.info("ScheduleAI logging initialised — app=%s  errors=%s", APP_LOG, ERROR_LOG)


_setup_root()


def get_logger(name: str) -> logging.Logger:
    """Return a named logger (call with __name__ in each module)."""
    return logging.getLogger(name)
