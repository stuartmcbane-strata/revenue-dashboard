"""Pull every Pipedrive deal (plus stages, pipelines, users, deal fields and
Pipedrive's own currency conversion rates) into Snowflake.

Runs inside Snowflake as the PIPEDRIVE.INTEGRATION.SYNC_PIPEDRIVE() procedure
(handler `run`, token from the PIPEDRIVE_API_TOKEN secret) and locally via
scripts/sync_local.py with a Snowpark session and $PIPEDRIVE_API_TOKEN.

Each table is rebuilt in a load table and swapped in, so the dashboard never
sees a half-written table.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import pandas as pd
import requests

API = "https://api.pipedrive.com"
PAGE = 500
CUSTOM_KEY = re.compile(r"^[0-9a-f]{40}$")

DEAL_COLUMNS = [
    "id", "title", "status", "pipeline_id", "stage_id", "value", "currency",
    "weighted_value", "probability", "expected_close_date", "add_time", "update_time",
    "stage_change_time", "won_time", "lost_time", "close_time", "local_won_date",
    "local_lost_date", "local_close_date", "lost_reason", "origin", "channel",
    "products_count", "next_activity_date", "last_activity_date", "visible_to",
    "is_archived",
]


def _get(session: requests.Session, path: str, token: str, **params) -> dict:
    for attempt in range(5):
        resp = session.get(f"{API}{path}", params={"api_token": token, **params}, timeout=60)
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 401:
            raise RuntimeError("Pipedrive rejected the API token (401): update the PIPEDRIVE_API_TOKEN secret")
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success", False):
            raise RuntimeError(f"Pipedrive {path} failed: {body}")
        return body
    raise RuntimeError(f"Pipedrive {path} kept failing ({resp.status_code}): {resp.text[:300]}")


def _paged(session: requests.Session, path: str, token: str, **params) -> list[dict]:
    rows, start = [], 0
    while True:
        body = _get(session, path, token, start=start, limit=PAGE, **params)
        rows += body.get("data") or []
        page = (body.get("additional_data") or {}).get("pagination") or {}
        if not page.get("more_items_in_collection"):
            return rows
        start = page["next_start"]


def fetch(token: str) -> dict:
    """Everything the dashboard needs, straight from the Pipedrive v1 API."""
    with requests.Session() as http:
        return {
            "deals": _paged(http, "/v1/deals", token, status="all_not_deleted"),
            "stages": _get(http, "/v1/stages", token)["data"] or [],
            "pipelines": _get(http, "/v1/pipelines", token)["data"] or [],
            "users": _get(http, "/v1/users", token)["data"] or [],
            "deal_fields": _paged(http, "/v1/dealFields", token),
            "summary": _get(http, "/v1/deals/summary", token)["data"] or {},
            "me": _get(http, "/v1/users/me", token)["data"] or {},
        }


def _name(value):
    if isinstance(value, dict):
        return value.get("name") or value.get("value")
    return value


def custom_fields(deal: dict, fields: list[dict]) -> dict:
    """{field name: decoded value} for the company's own deal fields; option ids
    become labels and multi-option (set) fields become lists."""
    out = {}
    for field in fields:
        key = field["key"]
        if not CUSTOM_KEY.match(key) or key not in deal:
            continue
        raw = deal[key]
        options = {str(o["id"]): o["label"] for o in field.get("options") or []}
        if raw in (None, ""):
            value = None
        elif field["field_type"] == "set":
            value = [options.get(v.strip(), v.strip()) for v in str(raw).split(",") if v.strip()]
        elif field["field_type"] == "enum":
            value = options.get(str(raw), str(raw))
        else:
            value = _name(raw)
        out[field["name"]] = value
    return out


def labels(deal: dict, fields: list[dict]) -> list[str]:
    field = next((f for f in fields if f["key"] == "label"), None)
    options = {str(o["id"]): o["label"] for o in (field or {}).get("options") or []}
    raw = deal.get("label")
    if raw in (None, ""):
        return []
    return [options.get(v.strip(), v.strip()) for v in str(raw).split(",") if v.strip()]


def deal_rows(data: dict) -> pd.DataFrame:
    fields = data["deal_fields"]
    rows = []
    for deal in data["deals"]:
        row = {c.upper(): deal.get(c) for c in DEAL_COLUMNS}
        owner = deal.get("user_id") or {}
        org = deal.get("org_id") or {}
        person = deal.get("person_id") or {}
        row.update(
            OWNER_ID=owner.get("id") if isinstance(owner, dict) else owner,
            OWNER_NAME=deal.get("owner_name") or _name(owner),
            ORG_ID=org.get("value") if isinstance(org, dict) else org,
            ORG_NAME=deal.get("org_name") or _name(org),
            PERSON_NAME=deal.get("person_name") or _name(person),
            LABELS=json.dumps(labels(deal, fields)),
            CUSTOM_FIELDS=json.dumps(custom_fields(deal, fields)),
            RAW=json.dumps(deal),
        )
        rows.append(row)
    return pd.DataFrame(rows, columns=[c.upper() for c in DEAL_COLUMNS] + [
        "OWNER_ID", "OWNER_NAME", "ORG_ID", "ORG_NAME", "PERSON_NAME", "LABELS",
        "CUSTOM_FIELDS", "RAW",
    ])


def stage_rows(data: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "STAGE_ID": s["id"], "PIPELINE_ID": s["pipeline_id"], "ORDER_NR": s["order_nr"],
                "STAGE_NAME": s["name"].strip(), "DEAL_PROBABILITY": s.get("deal_probability"),
                "ACTIVE": bool(s.get("active_flag", True)),
            }
            for s in data["stages"]
        ],
        columns=["STAGE_ID", "PIPELINE_ID", "ORDER_NR", "STAGE_NAME", "DEAL_PROBABILITY", "ACTIVE"],
    )


def pipeline_rows(data: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"PIPELINE_ID": p["id"], "PIPELINE_NAME": p["name"].strip(),
             "ORDER_NR": p.get("order_nr"), "ACTIVE": bool(p.get("active", True))}
            for p in data["pipelines"]
        ],
        columns=["PIPELINE_ID", "PIPELINE_NAME", "ORDER_NR", "ACTIVE"],
    )


def user_rows(data: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"USER_ID": u["id"], "USER_NAME": u.get("name"), "EMAIL": u.get("email"),
             "ACTIVE": bool(u.get("active_flag", True))}
            for u in data["users"]
        ],
        columns=["USER_ID", "USER_NAME", "EMAIL", "ACTIVE"],
    )


def field_rows(data: dict) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"FIELD_KEY": f["key"], "FIELD_NAME": f["name"], "FIELD_TYPE": f["field_type"],
             "IS_CUSTOM": bool(CUSTOM_KEY.match(f["key"])),
             "OPTIONS": json.dumps([o["label"] for o in f.get("options") or []])}
            for f in data["deal_fields"]
        ],
        columns=["FIELD_KEY", "FIELD_NAME", "FIELD_TYPE", "IS_CUSTOM", "OPTIONS"],
    )


def fx_rows(data: dict) -> pd.DataFrame:
    """Pipedrive's own conversion into the company currency, taken from the deals
    summary (value_converted / value per currency) so totals match Pipedrive."""
    base = (data.get("me") or {}).get("default_currency") or "GBP"
    totals = (data.get("summary") or {}).get("values_total") or {}
    rows = [{"CURRENCY": base, "BASE_CURRENCY": base, "RATE_TO_BASE": 1.0}]
    for currency, t in totals.items():
        if currency != base and t.get("value"):
            rows.append({"CURRENCY": currency, "BASE_CURRENCY": base,
                         "RATE_TO_BASE": float(t["value_converted"]) / float(t["value"])})
    return pd.DataFrame(rows, columns=["CURRENCY", "BASE_CURRENCY", "RATE_TO_BASE"])


TABLES = {
    "DEALS": deal_rows,
    "STAGES": stage_rows,
    "PIPELINES": pipeline_rows,
    "USERS": user_rows,
    "DEAL_FIELDS": field_rows,
    "FX_RATES": fx_rows,
}


def load(session, data: dict, database: str = "PIPEDRIVE", schema: str = "RAW") -> dict:
    """Write every table via a load table + SWAP; returns row counts."""
    counts = {}
    loaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for table, build in TABLES.items():
        frame = build(data)
        frame["LOADED_AT"] = loaded_at
        frame = frame.astype(object).where(frame.notna(), None)
        target = f"{database}.{schema}.{table}"
        staging = f"{table}__LOAD"
        session.write_pandas(
            frame, staging, database=database, schema=schema,
            auto_create_table=True, overwrite=True, quote_identifiers=False,
        )
        session.sql(f"CREATE TABLE IF NOT EXISTS {target} LIKE {database}.{schema}.{staging}").collect()
        session.sql(f"ALTER TABLE {target} SWAP WITH {database}.{schema}.{staging}").collect()
        session.sql(f"DROP TABLE IF EXISTS {database}.{schema}.{staging}").collect()
        counts[table] = len(frame)
    return counts


def _log(session, database: str, started: datetime, status: str, detail: str) -> None:
    session.sql(
        f"INSERT INTO {database}.INTEGRATION.SYNC_LOG (STARTED_AT, FINISHED_AT, STATUS, DETAIL) "
        "SELECT ?, CURRENT_TIMESTAMP(), ?, ?",
        params=[started.strftime("%Y-%m-%d %H:%M:%S +00:00"), status, detail[:4000]],
    ).collect()


def sync(session, token: str, database: str = "PIPEDRIVE") -> str:
    started = datetime.now(timezone.utc)
    try:
        counts = load(session, fetch(token), database)
    except Exception as exc:
        _log(session, database, started, "ERROR", f"{type(exc).__name__}: {exc}")
        raise
    detail = json.dumps(counts)
    _log(session, database, started, "OK", detail)
    return detail


def run(session) -> str:
    """Snowflake procedure handler."""
    import _snowflake  # only available inside Snowflake

    return sync(session, _snowflake.get_generic_secret_string("pipedrive_token"))
