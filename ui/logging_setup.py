"""Structured logging configuration for regime_trader."""

import logging


def setup_logging(log_level=logging.INFO, log_file: str = None) -> logging.Logger:
    """
    Configure structured logging for regime_trader.

    Format: "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"

    - Console handler: always attached
    - File handler: attached only if log_file is not None
    - Both use the same format
    - Suppress noisy third-party loggers: hmmlearn, alpaca, urllib3

    Returns the root logger for regime_trader: logging.getLogger("regime_trader")
    """
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"
    formatter = logging.Formatter(fmt)

    root = logging.getLogger("regime_trader")
    root.setLevel(log_level)

    # Avoid adding duplicate handlers if called more than once
    if not root.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    if log_file is not None:
        # Only add a new file handler if one pointing to the same file isn't already attached
        existing_files = {
            h.baseFilename
            for h in root.handlers
            if isinstance(h, logging.FileHandler)
        }
        if log_file not in existing_files:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

    # Suppress noisy third-party loggers
    for noisy in ("hmmlearn", "alpaca", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root
