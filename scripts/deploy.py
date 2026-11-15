"""Create/refresh everything in the PIPEDRIVE database (after the one-off
snowflake/00_admin_setup.sql has been run as ACCOUNTADMIN):

    python scripts/deploy.py                 # tables, procedure, task, Streamlit app
    python scripts/deploy.py --sync          # ... then run the Pipedrive sync once
    python scripts/deploy.py --app-only      # just re-upload the Streamlit app

$PIPEDRIVE_API_TOKEN (if set) is stored in the PIPEDRIVE_API_TOKEN Snowflake secret.
Connection settings come from app/.streamlit/secrets.toml ([connections.snowflake]).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import snowflake.connector
import toml

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
DB = "PIPEDRIVE"
CODE_STAGE = f"{DB}.SYNC.CODE"
APP_STAGE = f"{DB}.APP.DASHBOARD_STAGE"
APP = f"{DB}.APP.PIPEDRIVE_REVENUE_DASHBOARD"
PROC = f"{DB}.SYNC.SYNC_PIPEDRIVE"
TASK = f"{DB}.SYNC.SYNC_PIPEDRIVE_HOURLY"
SCHEDULE = "USING CRON 0 * * * MON-FRI Europe/London"
VIEWER_ROLE = "PIPEDRIVE_DASHBOARD_VIEWER"  # granted to the SCIM role for Entra group AAD-SNOWFLAKE_PIPEDRIVE_DASHBOARD
APP_FILES = [
    "streamlit_app.py",
    "environment.yml",
    ".streamlit/config.toml",
    *[str(p.relative_to(APP_DIR)) for p in sorted((APP_DIR / "prd").glob("*.py"))],
    *[str(p.relative_to(APP_DIR)) for p in sorted((APP_DIR / "assets").glob("*"))],
]


def connect():
    settings = toml.load(APP_DIR / ".streamlit" / "secrets.toml")
    conn = dict(settings["connections"]["snowflake"])
    if "private_key_file" in conn:
        conn["private_key_file"] = os.path.expanduser(conn["private_key_file"])
    return snowflake.connector.connect(**conn), conn["warehouse"]


def put(cur, local: Path, stage: str, subdir: str = "") -> None:
    target = f"@{stage}/{subdir}" if subdir else f"@{stage}"
    cur.execute(f"PUT 'file://{local}' '{target}' OVERWRITE = TRUE AUTO_COMPRESS = FALSE")


def deploy_pipeline(conn, cur, warehouse: str) -> None:
    for c in conn.execute_string((ROOT / "snowflake" / "01_objects.sql").read_text()):
        c.close()
    print("tables + views ready")

    token = os.environ.get("PIPEDRIVE_API_TOKEN")
    if token:
        escaped = token.replace("\\", "\\\\").replace("'", "\\'")
        cur.execute(
            f"CREATE OR REPLACE SECRET {DB}.SYNC.PIPEDRIVE_API_TOKEN "
            f"TYPE = GENERIC_STRING SECRET_STRING = '{escaped}' "
            "COMMENT = 'Pipedrive personal API token (admin user)'"
        )
        print("secret PIPEDRIVE_API_TOKEN stored")

    cur.execute(f"CREATE STAGE IF NOT EXISTS {CODE_STAGE}")
    put(cur, ROOT / "pipedrive_sync" / "sync.py", CODE_STAGE)
    cur.execute(f"""
        CREATE OR REPLACE PROCEDURE {PROC}()
        RETURNS STRING
        LANGUAGE PYTHON
        RUNTIME_VERSION = '3.11'
        PACKAGES = ('snowflake-snowpark-python', 'requests', 'pandas', 'pyarrow')
        IMPORTS = ('@{CODE_STAGE}/sync.py')
        HANDLER = 'sync.run'
        EXTERNAL_ACCESS_INTEGRATIONS = (PIPEDRIVE_API_INTEGRATION)
        SECRETS = ('pipedrive_token' = {DB}.SYNC.PIPEDRIVE_API_TOKEN)
        COMMENT = 'Full refresh of PIPEDRIVE.RAW from the Pipedrive API'
        EXECUTE AS OWNER
    """)
    print(f"procedure {PROC} created")

    cur.execute(f"""
        CREATE OR REPLACE TASK {TASK}
        WAREHOUSE = {warehouse}
        SCHEDULE = '{SCHEDULE}'
        USER_TASK_TIMEOUT_MS = 600000
        COMMENT = 'Hourly Pipedrive refresh, Monday-Friday (UK time)'
        AS CALL {PROC}()
    """)
    cur.execute(f"ALTER TASK {TASK} RESUME")
    print(f"task {TASK} resumed: {SCHEDULE}")


def deploy_app(cur, warehouse: str) -> None:
    cur.execute(f"CREATE STAGE IF NOT EXISTS {APP_STAGE} DIRECTORY = (ENABLE = TRUE)")
    for name in APP_FILES:
        parent = str(Path(name).parent)
        put(cur, APP_DIR / name, APP_STAGE, "" if parent == "." else parent)
    cur.execute(
        f"CREATE OR REPLACE STREAMLIT {APP} ROOT_LOCATION = '@{APP_STAGE}' "
        f"MAIN_FILE = '/streamlit_app.py' QUERY_WAREHOUSE = {warehouse} "
        "TITLE = 'Pipedrive Revenue Dashboard' "
        "COMMENT = 'Pipedrive revenue by month and deal stage vs target'"
    )
    # CREATE OR REPLACE drops grants on the app, so re-grant the viewer role (once it exists).
    try:
        for grant in (f"DATABASE {DB}", f"SCHEMA {DB}.APP", f"STREAMLIT {APP}"):
            cur.execute(f"GRANT USAGE ON {grant} TO ROLE {VIEWER_ROLE}")
        print(f"granted to {VIEWER_ROLE}")
    except snowflake.connector.errors.ProgrammingError as exc:
        print(f"viewer grants skipped ({VIEWER_ROLE}): {exc.msg}")
    cur.execute(f"SHOW STREAMLITS LIKE 'PIPEDRIVE_REVENUE_DASHBOARD' IN SCHEMA {DB}.APP")
    print("streamlit:", cur.fetchall())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync", action="store_true", help="run the Pipedrive sync after deploying")
    parser.add_argument("--app-only", action="store_true", help="only re-upload the Streamlit app")
    args = parser.parse_args()
    conn, warehouse = connect()
    cur = conn.cursor()
    if not args.app_only:
        deploy_pipeline(conn, cur, warehouse)
    deploy_app(cur, warehouse)
    if args.sync:
        cur.execute(f"CALL {PROC}()")
        print("sync:", cur.fetchone()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
