"""Load `.env`, without a dependency and without surprises.

Every document in this repository tells you to put your Razorpay keys in `.env`,
and `make setup` creates one for you. For a while nothing actually read it — so
the one live check a reviewer runs would have failed with a traceback telling
them to do the thing they had already done. This module is that gap closed.

Deliberately not `python-dotenv`. The whole job is twenty lines of parsing, and a
dependency that reads a file containing API keys is a dependency worth not
having. What it does:

* **Real environment variables win.** A key already in ``os.environ`` is never
  overwritten, so ``PAYMENT_RAIL=razorpay make demo`` and CI secrets both behave
  the way anyone would expect, and ``demo/batch.py --live`` can set the rail
  before the file is read.
* **No evaluation of any kind.** Split on the first ``=``, strip whitespace,
  strip one layer of matching quotes. No shell, no ``eval``, no interpolation, no
  multi-line values. A `.env` cannot execute anything.
* **A missing file is not an error.** Deployments that inject real environment
  variables have no `.env`, and that is the normal case in production.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_PATH = Path(".env")

#: `export FOO=bar` is common in hand-written .env files. We accept it rather
#: than silently creating a variable literally named "export FOO".
_EXPORT_PREFIX = "export "


def parse_env(text: str) -> dict[str, str]:
    """Parse `.env` text into a mapping. Pure, so it is directly testable."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith(_EXPORT_PREFIX):
            line = line[len(_EXPORT_PREFIX) :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Strip exactly one layer of matching quotes; anything else is literal.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv(path: str | Path = DEFAULT_ENV_PATH, *, override: bool = False) -> list[str]:
    """Load `.env` into ``os.environ``. Returns the names it actually set.

    Returns names, never values — so a caller can log "loaded 4 variables from
    .env" without a key ever reaching a log line.
    """
    resolved = Path(path)
    if not resolved.is_file():
        return []
    applied: list[str] = []
    for key, value in parse_env(resolved.read_text(encoding="utf-8")).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


class ConfigurationError(RuntimeError):
    """A configuration problem a human can fix, reported without a traceback.

    Raised for "you have not set your Razorpay keys", not for a bug. Entry points
    catch it, print the message, and exit non-zero — a stack trace here would bury
    the one sentence that tells the reader what to do.
    """
