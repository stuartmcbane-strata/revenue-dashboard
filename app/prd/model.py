"""Deal model: one row per Pipedrive deal with company-currency values,
probabilities, revenue month and Strata financial year (1 Feb - 31 Jan)."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd

FY_START_MONTH = 2  # Strata's financial year runs 1 Feb - 31 Jan
WON_DATE, EXPECTED_DATE = "Won date", "Expected close date"
FULL, WEIGHTED = "Full value", "Weighted value"
PIPEDRIVE_URL = "https://strata.pipedrive.com/deal/{}"
NO_VALUE = "(Blank)"


def fy_start_year(day) -> int:
    day = pd.Timestamp(day)
    return day.year if day.month >= FY_START_MONTH else day.year - 1


def fy_label(start_year: int) -> str:
    return f"FY{start_year}/{str(start_year + 1)[-2:]}"


def fy_months(start_year: int) -> list[pd.Timestamp]:
    first = pd.Timestamp(year=start_year, month=FY_START_MONTH, day=1)
    return [first + pd.DateOffset(months=i) for i in range(12)]


def fy_range_text(start_year: int) -> str:
    months = fy_months(start_year)
    return f"{months[0]:%b %Y} – {months[-1]:%b %Y}"


def _json_list(value) -> list:
    if value in (None, "") or (isinstance(value, float) and pd.isna(value)):
        return []
    parsed = json.loads(value) if isinstance(value, str) else value
    return list(parsed or [])


def _json_dict(value) -> dict:
    if value in (None, "") or (isinstance(value, float) and pd.isna(value)):
        return {}
    return dict(json.loads(value) if isinstance(value, str) else value)


def _day(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize()


def custom_dimensions(fields: pd.DataFrame) -> list[str]:
    """Company-defined single/multi-option deal fields: each becomes a filter."""
    if fields.empty:
        return []
    keep = fields[fields["IS_CUSTOM"].astype(bool) & fields["FIELD_TYPE"].isin(["enum", "set"])]
    return list(keep["FIELD_NAME"])


def fx_table(rates: pd.DataFrame, overrides: pd.DataFrame) -> pd.DataFrame:
    """Rate into the company currency per currency: manual override, else Pipedrive's."""
    out = rates[["CURRENCY", "RATE_TO_BASE"]].rename(columns={"RATE_TO_BASE": "PIPEDRIVE_RATE"})
    if overrides is not None and not overrides.empty:
        out = out.merge(
            overrides[["CURRENCY", "RATE_TO_BASE"]].rename(columns={"RATE_TO_BASE": "OVERRIDE_RATE"}),
            on="CURRENCY", how="outer",
        )
    else:
        out["OVERRIDE_RATE"] = None
    out["PIPEDRIVE_RATE"] = pd.to_numeric(out["PIPEDRIVE_RATE"], errors="coerce")
    out["OVERRIDE_RATE"] = pd.to_numeric(out["OVERRIDE_RATE"], errors="coerce")
    out["RATE"] = out["OVERRIDE_RATE"].fillna(out["PIPEDRIVE_RATE"])
    return out


def build_deals(
    deals: pd.DataFrame,
    stages: pd.DataFrame,
    pipelines: pd.DataFrame,
    fx: pd.DataFrame,
    dimensions: list[str],
) -> pd.DataFrame:
    df = deals.copy()
    for col in ["ID", "PIPELINE_ID", "STAGE_ID"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df["VALUE"] = pd.to_numeric(df["VALUE"], errors="coerce").fillna(0.0)

    st_ = stages.copy()
    st_["STAGE_ID"] = pd.to_numeric(st_["STAGE_ID"]).astype("Int64")
    st_["PIPELINE_ID"] = pd.to_numeric(st_["PIPELINE_ID"]).astype("Int64")
    pl = pipelines.copy()
    pl["PIPELINE_ID"] = pd.to_numeric(pl["PIPELINE_ID"]).astype("Int64")
    pl = pl.rename(columns={"ORDER_NR": "PIPELINE_ORDER"})
    df = df.merge(
        st_[["STAGE_ID", "STAGE_NAME", "ORDER_NR", "DEAL_PROBABILITY"]].rename(
            columns={"ORDER_NR": "STAGE_ORDER", "DEAL_PROBABILITY": "STAGE_PROBABILITY"}
        ),
        on="STAGE_ID", how="left",
    ).merge(pl[["PIPELINE_ID", "PIPELINE_NAME", "PIPELINE_ORDER"]], on="PIPELINE_ID", how="left")
    df["STAGE_NAME"] = df["STAGE_NAME"].fillna("(Unknown stage)")
    df["PIPELINE_NAME"] = df["PIPELINE_NAME"].fillna("(Unknown pipeline)")

    rates = dict(zip(fx["CURRENCY"], fx["RATE"]))
    df["FX_RATE"] = df["CURRENCY"].map(rates)
    df["VALUE_BASE"] = df["VALUE"] * df["FX_RATE"]

    stage_prob = pd.to_numeric(df["STAGE_PROBABILITY"], errors="coerce")
    deal_prob = pd.to_numeric(df["PROBABILITY"], errors="coerce")
    df["PROB"] = deal_prob.fillna(stage_prob).fillna(0.0)
    df.loc[df["STATUS"] == "won", "PROB"] = 100.0
    df.loc[df["STATUS"] == "lost", "PROB"] = 0.0
    df["WEIGHTED_BASE"] = df["VALUE_BASE"] * df["PROB"] / 100.0

    df["EXPECTED_CLOSE"] = _day(df["EXPECTED_CLOSE_DATE"])
    df["WON_DATE"] = _day(df["LOCAL_WON_DATE"].fillna(df["WON_TIME"]))
    df["LOST_DATE"] = _day(df["LOCAL_LOST_DATE"].fillna(df["LOST_TIME"]))
    df["ADDED"] = _day(df["ADD_TIME"])

    df["SERVICE_LINES"] = df["LABELS"].map(_json_list)
    custom = df["CUSTOM_FIELDS"].map(_json_dict)
    for name in dimensions:
        df[name] = custom.map(lambda c, n=name: c.get(n))
    df["STATUS_LABEL"] = df["STATUS"].str.title()
    df["OWNER_NAME"] = df["OWNER_NAME"].fillna(NO_VALUE)
    df["ORG_NAME"] = df["ORG_NAME"].fillna(NO_VALUE)
    df["URL"] = df["ID"].map(lambda i: PIPEDRIVE_URL.format(i))
    return df


def revenue_dates(df: pd.DataFrame, won_basis: str = WON_DATE) -> pd.Series:
    """Date a deal's revenue falls in: won deals on their won date (or expected
    close date), open deals on their expected close date, lost on their lost date."""
    won = df["WON_DATE"] if won_basis == WON_DATE else df["EXPECTED_CLOSE"].fillna(df["WON_DATE"])
    out = df["EXPECTED_CLOSE"].copy()
    out[df["STATUS"] == "won"] = won[df["STATUS"] == "won"]
    out[df["STATUS"] == "lost"] = df["LOST_DATE"][df["STATUS"] == "lost"]
    return out


def with_revenue_month(df: pd.DataFrame, won_basis: str = WON_DATE) -> pd.DataFrame:
    df = df.copy()
    df["REVENUE_DATE"] = revenue_dates(df, won_basis)
    df["MONTH"] = df["REVENUE_DATE"].dt.to_period("M").dt.to_timestamp()
    df["FY"] = df["REVENUE_DATE"].map(lambda d: fy_start_year(d) if pd.notna(d) else pd.NA)
    return df


def stage_order(df: pd.DataFrame) -> pd.DataFrame:
    """Stages most-certain first (bottom of the stack), then pipeline/stage order."""
    cols = ["STAGE_ID", "STAGE_NAME", "PIPELINE_ID", "PIPELINE_NAME", "PIPELINE_ORDER",
            "STAGE_ORDER", "STAGE_PROBABILITY"]
    stages = df[cols].drop_duplicates("STAGE_ID").copy()
    stages["STAGE_PROBABILITY"] = pd.to_numeric(stages["STAGE_PROBABILITY"], errors="coerce").fillna(0)
    return stages.sort_values(
        ["PIPELINE_ORDER", "STAGE_PROBABILITY", "STAGE_ORDER"], ascending=[True, False, False]
    ).reset_index(drop=True)


def monthly(df: pd.DataFrame, months: list[pd.Timestamp], measure: str) -> pd.DataFrame:
    """Value per (MONTH, STAGE_ID) for every FY month, zero-filled."""
    grid = df.groupby(["MONTH", "STAGE_ID"], as_index=False).agg(
        VALUE=(measure, "sum"), DEALS=("ID", "count")
    )
    stages = df["STAGE_ID"].dropna().unique()
    full = pd.MultiIndex.from_product([months, stages], names=["MONTH", "STAGE_ID"]).to_frame(index=False)
    out = full.merge(grid, on=["MONTH", "STAGE_ID"], how="left")
    out["VALUE"] = out["VALUE"].fillna(0.0)
    out["DEALS"] = out["DEALS"].fillna(0).astype(int)
    return out


def cumulative(grid: pd.DataFrame) -> pd.DataFrame:
    out = grid.sort_values(["STAGE_ID", "MONTH"]).copy()
    out["VALUE"] = out.groupby("STAGE_ID")["VALUE"].cumsum()
    out["DEALS"] = out.groupby("STAGE_ID")["DEALS"].cumsum()
    return out.sort_values(["MONTH", "STAGE_ID"]).reset_index(drop=True)


def target_series(targets: pd.DataFrame, months: list[pd.Timestamp]) -> pd.Series:
    """Target per FY month (NaN where none is loaded)."""
    if targets is None or targets.empty:
        return pd.Series([float("nan")] * len(months), index=months)
    t = targets.copy()
    t["MONTH_START"] = pd.to_datetime(t["MONTH_START"]).dt.to_period("M").dt.to_timestamp()
    t["TARGET"] = pd.to_numeric(t["TARGET"], errors="coerce")
    by_month = t.groupby("MONTH_START")["TARGET"].sum()
    return by_month.reindex(months)


def parse_targets(raw: pd.DataFrame) -> pd.DataFrame:
    """Uploaded targets -> MONTH_START, TARGET. Accepts a Month column (any common
    date format, e.g. 2026-02, Feb-26, 01/02/2026) and a Target column; or one
    row with a column per month."""
    cols = {str(c).strip().lower(): c for c in raw.columns}
    month_col = next((cols[c] for c in cols if c in ("month", "month_start", "period", "date")), None)
    target_col = next((cols[c] for c in cols if "target" in c or c in ("value", "revenue", "amount")), None)
    if month_col is not None and target_col is not None:
        months = raw[month_col]
        values = raw[target_col]
    else:
        # Wide layout: month names as headers, first row of figures.
        parsed = {c: _month(c) for c in raw.columns}
        month_cols = [c for c, m in parsed.items() if m is not None]
        if not month_cols:
            raise ValueError("Couldn't find a Month and a Target column (or month headers).")
        months = pd.Series([parsed[c] for c in month_cols])
        values = pd.Series([raw[c].iloc[0] for c in month_cols])
    out = pd.DataFrame({"MONTH_START": [_month(m) for m in months], "TARGET": [_money(v) for v in values]})
    bad = out["MONTH_START"].isna()
    if bad.any():
        raise ValueError(f"Unrecognised month(s): {', '.join(str(m) for m in months[bad.values])}")
    out = out.dropna(subset=["TARGET"])
    return out.groupby("MONTH_START", as_index=False)["TARGET"].sum().sort_values("MONTH_START")


def _money(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("£", "").replace("$", "")
    mult = 1.0
    if text[-1:].lower() in ("k", "m"):
        mult = 1e3 if text[-1].lower() == "k" else 1e6
        text = text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return None


def _month(value) -> pd.Timestamp | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (pd.Timestamp, date)):
        return pd.Timestamp(value).to_period("M").to_timestamp()
    text = str(value).strip()
    for fmt in ("%Y-%m", "%Y-%m-%d", "%b-%y", "%b %y", "%b-%Y", "%b %Y", "%B %Y", "%B-%y",
                "%d/%m/%Y", "%m/%Y", "%Y/%m", "%Y-%m-%d %H:%M:%S"):
        try:
            return pd.to_datetime(text, format=fmt).to_period("M").to_timestamp()
        except (ValueError, TypeError):
            continue
    return None


def fmt_money(value: float, symbol: str = "£") -> str:
    if value is None or pd.isna(value):
        return "–"
    sign = "-" if value < 0 else ""
    v = abs(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= size:
            return f"{sign}{symbol}{v / size:,.2f}{suffix}"
    return f"{sign}{symbol}{v:,.0f}"
