import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def safe_remove(*file_paths: Path | str | None):
    """Safely delete one or more files if they exist."""
    for path in file_paths:
        if not path:
            continue
        try:
            p = Path(path)
            if p.exists() and p.is_file():
                p.unlink()
                logger.debug(f"Removed temporary file: {p}")
        except Exception as e:
            logger.warning(f"Failed to remove file {path}: {e}")
