"""Snowflake access: Streamlit in Snowflake's session, or locally the
[connections.snowflake] entry in .streamlit/secrets.toml."""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st

DATABASE = os.environ.get("PRD_DATABASE", "PIPEDRIVE")
RAW = f"{DATABASE}.RAW"
APP = f"{DATABASE}.APP"
INTEGRATION = f"{DATABASE}.INTEGRATION"
CACHE_TTL = 600


def session():
    try:
        from snowflake.snowpark.context import get_active_session

        return get_active_session()
    except Exception:
        return st.connection("snowflake").session()


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    return session().sql(sql, params=params).to_pandas()


OFFLINE = os.environ.get("PRD_OFFLINE_FETCH")  # dev only: a JSON dump of sync.fetch()


def _offline_tables() -> dict[str, pd.DataFrame]:
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from pipedrive_sync import sync

    fetched = json.loads(Path(OFFLINE).read_text())
    targets = Path(OFFLINE).with_suffix(".targets.csv")
    return {
        "deals": sync.deal_rows(fetched), "stages": sync.stage_rows(fetched),
        "pipelines": sync.pipeline_rows(fetched), "fields": sync.field_rows(fetched),
        "fx": sync.fx_rows(fetched),
        "fx_overrides": pd.DataFrame(columns=["CURRENCY", "RATE_TO_BASE"]),
        "targets": pd.read_csv(targets) if targets.exists() else pd.DataFrame(columns=["MONTH_START", "TARGET"]),
        "last_sync": pd.DataFrame([{"FINISHED_AT": pd.Timestamp.now(tz="UTC"), "STATUS": "OFFLINE", "DETAIL": ""}]),
    }


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading Pipedrive deals…")
def raw_tables(version: int) -> dict[str, pd.DataFrame]:
    """`version` busts the cache after a refresh or a save."""
    if OFFLINE:
        return _offline_tables()
    return {
        "deals": query(f"SELECT * FROM {RAW}.DEALS"),
        "stages": query(f"SELECT * FROM {RAW}.STAGES"),
        "pipelines": query(f"SELECT * FROM {RAW}.PIPELINES"),
        "fields": query(f"SELECT * FROM {RAW}.DEAL_FIELDS"),
        "fx": query(f"SELECT * FROM {RAW}.FX_RATES"),
        "fx_overrides": query(f"SELECT CURRENCY, RATE_TO_BASE FROM {APP}.FX_OVERRIDES"),
        "targets": query(f"SELECT MONTH_START, TARGET FROM {APP}.REVENUE_TARGETS"),
        "last_sync": query(
            f"SELECT FINISHED_AT, STATUS, DETAIL FROM {INTEGRATION}.SYNC_LOG "
            "ORDER BY FINISHED_AT DESC LIMIT 1"
        ),
    }


def save_targets(months: list[pd.Timestamp], targets: pd.DataFrame, user: str) -> None:
    """Replace the targets for `months` with `targets` (MONTH_START, TARGET)."""
    if OFFLINE:
        from pathlib import Path

        path = Path(OFFLINE).with_suffix(".targets.csv")
        old = pd.read_csv(path, parse_dates=["MONTH_START"]) if path.exists() else pd.DataFrame(columns=["MONTH_START", "TARGET"])
        old = old[~pd.to_datetime(old["MONTH_START"]).between(min(months), max(months))]
        pd.concat([old, targets[["MONTH_START", "TARGET"]]]).dropna().to_csv(path, index=False)
        return
    s = session()
    first, last = min(months), max(months)
    s.sql(
        f"DELETE FROM {APP}.REVENUE_TARGETS WHERE MONTH_START BETWEEN ? AND ?",
        params=[first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")],
    ).collect()
    for row in targets.itertuples(index=False):
        if pd.isna(row.TARGET):
            continue
        s.sql(
            f"INSERT INTO {APP}.REVENUE_TARGETS (MONTH_START, TARGET, UPDATED_BY) "
            "SELECT ?, ?, ?",
            params=[pd.Timestamp(row.MONTH_START).strftime("%Y-%m-%d"), float(row.TARGET), user],
        ).collect()


def save_fx_overrides(overrides: pd.DataFrame, user: str) -> None:
    s = session()
    s.sql(f"DELETE FROM {APP}.FX_OVERRIDES").collect()
    for row in overrides.itertuples(index=False):
        if pd.isna(row.RATE_TO_BASE):
            continue
        s.sql(
            f"INSERT INTO {APP}.FX_OVERRIDES (CURRENCY, RATE_TO_BASE, UPDATED_BY) SELECT ?, ?, ?",
            params=[row.CURRENCY, float(row.RATE_TO_BASE), user],
        ).collect()


def refresh_now() -> str:
    return session().sql(f"CALL {INTEGRATION}.SYNC_PIPEDRIVE()").collect()[0][0]


def current_user() -> str:
    try:
        return st.experimental_user.get("user_name") or st.experimental_user.get("email") or "app"
    except Exception:
        return "app"
