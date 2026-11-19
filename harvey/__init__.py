"""Xorora Prospector — an autonomous sales agent that runs on your Claude subscription."""

import sys

# Fail with a sentence someone can act on, not a SyntaxError from deep inside
# a module. `python3 -m venv` on macOS builds from the system 3.9, so this is
# the single most likely first-run problem.
if sys.version_info < (3, 11):  # pragma: no cover - version guard
    raise RuntimeError(
        f"Xorora Prospector needs Python 3.11+, but this is "
        f"{sys.version_info.major}.{sys.version_info.minor} "
        f"({sys.executable}). On macOS `python3` is the system 3.9 — rebuild "
        f"the venv from a newer Python:\n"
        f"    brew install python@3.13\n"
        f"    rm -rf .venv && $(brew --prefix)/bin/python3.13 -m venv .venv\n"
        f"    source .venv/bin/activate && pip install -e ."
    )

"""Xorora Prospector — Autonomous Sales Agent."""

# Logs read "[xorora-prospector.sender]", not the internal package name.
import logging as _logging

_base_factory = _logging.getLogRecordFactory()


def _branded_record(*args, **kwargs):
    record = _base_factory(*args, **kwargs)
    if record.name == "harvey" or record.name.startswith("harvey."):
        record.name = "xorora-prospector" + record.name[len("harvey"):]
    return record


_logging.setLogRecordFactory(_branded_record)
