"""Run the Pipedrive sync from this machine instead of inside Snowflake:

    PIPEDRIVE_API_TOKEN=... python scripts/sync_local.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import toml
from snowflake.snowpark import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipedrive_sync.sync import sync  # noqa: E402


def main() -> int:
    conn = dict(toml.load(ROOT / "app" / ".streamlit" / "secrets.toml")["connections"]["snowflake"])
    if "private_key_file" in conn:
        conn["private_key_file"] = os.path.expanduser(conn["private_key_file"])
    session = Session.builder.configs(conn).create()
    print(sync(session, os.environ["PIPEDRIVE_API_TOKEN"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
