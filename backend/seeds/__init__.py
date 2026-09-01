"""Seed data for the live demo, and the loader that applies it.

What is in ``seeds/*.json`` is not test fixture data -- it is the dataset that
appears on stage. Each record is there to make one specific behaviour
demonstrable; the comments in the JSON files say which.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["DEV_PASSWORD", "SEEDS_DIR", "seed_password"]

SEEDS_DIR = Path(__file__).resolve().parent

# Documented dev default. Never a production-looking string, and never a real
# secret: every seeded account shares it, and that is only acceptable because
# this password can only ever exist on a developer's machine.
DEV_PASSWORD = "sutradhar-dev-password"


def seed_password() -> str:
    """Password for every seeded account.

    Overridable with SEED_USER_PASSWORD so a shared demo box is not running on
    a value published in this repository.
    """
    return os.environ.get("SEED_USER_PASSWORD") or DEV_PASSWORD
