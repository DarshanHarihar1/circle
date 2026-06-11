"""Central logging setup — writes everything to logs/circle.log (rotating) and
the console. LLM prompts/outputs are logged via the 'llm' logger in llm.py, so
they land in the same file.
"""
import logging
import logging.handlers
import os
import sys

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_FILE = os.path.join(LOG_DIR, "circle.log")

_FMT = logging.Formatter(
    "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def setup_logging(level: int = logging.INFO) -> None:
    """Attach a rotating file handler (+ console) to the root logger. Idempotent."""
    os.makedirs(LOG_DIR, exist_ok=True)

    # Make the console UTF-8 safe — Windows defaults to cp1252, which crashes the
    # StreamHandler on ₹, arrows, emoji, etc. that show up in LLM output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

    root = logging.getLogger()
    root.setLevel(level)

    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8",
        )
        fh.setFormatter(_FMT)
        root.addHandler(fh)

    # Keep console output too (uvicorn may already add one; only add if none).
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(_FMT)
        root.addHandler(sh)

    logging.getLogger(__name__).info("Logging initialised -> %s", LOG_FILE)
