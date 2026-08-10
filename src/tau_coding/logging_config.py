"""Debug logging configuration for Tau.

Configures Python's standard ``logging`` module based on the CLI ``--debug``
flag or the ``TAU_DEBUG`` environment variable. In print mode, log records are
written to stderr so stdout stays clean for structured output. In TUI mode,
records go to ``~/.tau/logs/tau.log`` because the Textual app owns the terminal.
"""

from __future__ import annotations

import logging
import sys
from os import environ

from tau_coding.paths import TauPaths

_ENV_FLAG = "TAU_DEBUG"

# Tau package loggers that receive debug output. Child loggers
# (e.g. ``tau_ai.openai_compatible``) inherit automatically.
_TAU_LOGGERS = ("tau_ai", "tau_agent", "tau_coding")

_configured = False


def is_debug_env_set(env: dict[str, str] | None = None) -> bool:
    """Return whether the ``TAU_DEBUG`` environment variable is truthy."""
    environment = env if env is not None else environ
    return environment.get(_ENV_FLAG, "").lower() in ("1", "true", "yes", "on")


def configure_debug_logging(
    *,
    tui_mode: bool = False,
    paths: TauPaths | None = None,
) -> None:
    """Enable DEBUG-level logging on Tau's package loggers.

    In TUI mode, records are written to ``~/.tau/logs/tau.log`` (the Textual
    app owns the terminal, so stderr is not usable). In print mode, records
    go to stderr so stdout stays clean for structured output.

    Idempotent: calling again after the first successful call is a no-op.
    """
    global _configured
    if _configured:
        return

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if tui_mode:
        resolved_paths = paths or TauPaths()
        log_file = resolved_paths.logs_dir / "tau.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)

    for name in _TAU_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)

    _configured = True


def reset_debug_logging() -> None:
    """Remove all handlers added by ``configure_debug_logging``.

    Intended for tests that need a clean slate between cases.
    """
    global _configured
    for name in _TAU_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.NOTSET)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
    _configured = False
