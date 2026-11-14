"""Pipedrive revenue dashboard (Streamlit in Snowflake)."""

from __future__ import annotations

import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from prd import charts, data, model, ui

st.set_page_config(page_title="Pipedrive Revenue", page_icon="assets/strata_mark.png", layout="wide")
ui.page_setup()

MONTHLY, CUMULATIVE = "Month by month", "Cumulative"
STATE = st.session_state
STATE.setdefault("version", 0)
STATE.setdefault("picks", set())
STATE.setdefault("pick_context", None)


def _bump() -> None:
    STATE.version += 1
    st.cache_data.clear()


# ---------------------------------------------------------------- data
tables = data.raw_tables(STATE.version)
if tables["deals"].empty:
    st.warning("No Pipedrive deals loaded yet. Run CALL PIPEDRIVE.SYNC.SYNC_PIPEDRIVE() or use Refresh.")
dimensions = model.custom_dimensions(tables["fields"])
fx = model.fx_table(tables["fx"], tables["fx_overrides"])
deals_all = model.build_deals(tables["deals"], tables["stages"], tables["pipelines"], fx, dimensions)
today = pd.Timestamp(datetime.now(ZoneInfo("Europe/London")).date())
this_fy = model.fy_start_year(today)

# ---------------------------------------------------------------- header
head = st.columns([6, 3, 1])
ui.title(head[0], "Pipedrive Revenue")
with head[1]:
    last = tables["last_sync"]
    if not last.empty:
        when = pd.Timestamp(last.iloc[0]["FINISHED_AT"])
        if when.tzinfo is None:
            when = when.tz_localize("UTC")
        status = last.iloc[0]["STATUS"]
        st.caption(
            f"Last Pipedrive refresh: {when.tz_convert('Europe/London'):%a %d %b %Y %H:%M} "
            f"({status}). Updates hourly Mon–Fri."
        )
    if st.button("Refresh from Pipedrive now"):
        with st.spinner("Pulling deals from Pipedrive…"):
            try:
                data.refresh_now()
            except Exception as exc:  # surface the Pipedrive/Snowflake error text
                st.error(f"Refresh failed: {exc}")
        _bump()
        st.rerun()
ui.logo(head[2])

controls = st.columns([3, 3, 3, 3])
fy = controls[0].radio(
    "Financial year (1 Feb – 31 Jan)",
    [this_fy, this_fy + 1],
    format_func=lambda y: f"{model.fy_label(y)} ({model.fy_range_text(y)})",
    horizontal=True,
)
view = controls[1].radio("Chart", [MONTHLY, CUMULATIVE], horizontal=True)
measure_name = controls[2].radio("Value", [model.FULL, model.WEIGHTED], horizontal=True,
                                 help="Weighted = value × deal probability (stage probability "
                                      "unless the deal has its own). Won = 100%.")
won_basis = controls[3].radio("Won deals dated by", [model.WON_DATE, model.EXPECTED_DATE],
                              horizontal=True,
                              help="Open deals always use their expected close date.")
measure = "VALUE_BASE" if measure_name == model.FULL else "WEIGHTED_BASE"
months = model.fy_months(fy)

# ---------------------------------------------------------------- filters
deals_all = model.with_revenue_month(deals_all, won_basis)


def _options(series: pd.Series) -> list[str]:
    values = set()
    for v in series:
        if isinstance(v, (list, tuple)):
            values.update(str(x) for x in v)
        elif v is None or (isinstance(v, float) and pd.isna(v)):
            values.add(model.NO_VALUE)
        else:
            values.add(str(v))
    return sorted(values, key=lambda s: (s == model.NO_VALUE, s.lower()))


def _match(series: pd.Series, picked: list[str]) -> pd.Series:
    picked = set(picked)

    def hit(v) -> bool:
        if isinstance(v, (list, tuple)):
            return bool(picked & {str(x) for x in v}) or (not v and model.NO_VALUE in picked)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return model.NO_VALUE in picked
        return str(v) in picked

    return series.map(hit)


with st.sidebar:
    st.header("Filters")
    pipelines = (
        tables["pipelines"].assign(ORDER_NR=lambda p: pd.to_numeric(p["ORDER_NR"], errors="coerce"))
        .sort_values("ORDER_NR")["PIPELINE_NAME"].tolist()
    )
    chosen_pipes = st.multiselect("Pipeline", pipelines, default=pipelines[:1], key="f_pipeline")
    statuses = st.multiselect("Status", ["Open", "Won", "Lost"], default=["Open", "Won"], key="f_status")
    filters = {
        "OWNER_NAME": "Deal owner",
        **{d: d for d in dimensions},
        "SERVICE_LINES": "Service line (label)",
        "ORG_NAME": "Organisation",
        "CURRENCY": "Deal currency",
        "ORIGIN": "Source origin",
    }
    scope = deals_all[deals_all["PIPELINE_NAME"].isin(chosen_pipes) & deals_all["STATUS_LABEL"].isin(statuses)]
    stage_names = model.stage_order(scope)["STAGE_NAME"].drop_duplicates().tolist() if not scope.empty else []
    chosen_stages = st.multiselect("Deal stage", stage_names, placeholder="All stages", key="f_stage")
    chosen = {}
    for col, label in filters.items():
        if col not in scope.columns:
            continue
        chosen[col] = st.multiselect(label, _options(scope[col]), placeholder="All", key=f"f_{col}")
    if st.button("Clear filters"):
        for key in [k for k in STATE.keys() if str(k).startswith("f_")]:
            del STATE[key]
        STATE.picks = set()
        st.rerun()

deals = scope
if chosen_stages:
    deals = deals[deals["STAGE_NAME"].isin(chosen_stages)]
for col, picked in chosen.items():
    if picked:
        deals = deals[_match(deals[col], picked)]

in_fy = deals[deals["FY"] == fy]
undated = deals[deals["MONTH"].isna() & (deals["STATUS"] == "open")]
target = model.target_series(tables["targets"], months)
target_total = target.sum(min_count=1)

# ---------------------------------------------------------------- summary cards
won = in_fy[in_fy["STATUS"] == "won"]
open_ = in_fy[in_fy["STATUS"] == "open"]
won_value = won["VALUE_BASE"].sum()
weighted_forecast = won_value + open_["WEIGHTED_BASE"].sum()
pipeline_total = won_value + open_["VALUE_BASE"].sum()
fmt = model.fmt_money


def _gap(value: float) -> tuple[str, str, str]:
    if pd.isna(target_total):
        return "–", "No target loaded", ""
    gap = target_total - value
    if gap > 0:
        return fmt(gap), f"{value / target_total:.0%} of target", "bad"
    return fmt(-gap), f"{value / target_total:.0%} of target — ahead", "good"


cards = st.columns(6)
ui.card(cards[0], fmt(target_total), f"{model.fy_label(fy)} target",
        "" if not pd.isna(target_total) else "Load targets below")
ui.card(cards[1], fmt(won_value), "Won", f"{len(won)} deal(s)")
v, sub, tone = _gap(won_value)
ui.card(cards[2], v, "Gap to target (won)" if tone != "good" else "Ahead of target (won)", sub, tone)
ui.card(cards[3], fmt(weighted_forecast), "Weighted forecast", "Won + open × probability")
v, sub, tone = _gap(weighted_forecast)
ui.card(cards[4], v, "Gap to target (weighted)" if tone != "good" else "Ahead of target (weighted)", sub, tone)
ui.card(cards[5], fmt(pipeline_total), "Total pipeline (unweighted)", f"{len(won) + len(open_)} deal(s)")

stages = model.stage_order(in_fy) if not in_fy.empty else model.stage_order(deals) if not deals.empty else pd.DataFrame()
styles = charts.stage_styles(stages) if not stages.empty else {}
if not stages.empty:
    st.markdown("**By stage** — value in each stage, and the running total from the most certain stage down")
    per_row = 6
    running = 0.0
    show_pipeline = len(chosen_pipes) > 1
    stage_rows = list(stages.itertuples(index=False))
    for start in range(0, len(stage_rows), per_row):
        cols = st.columns(per_row)
        for col, stage in zip(cols, stage_rows[start : start + per_row]):
            rows = in_fy[in_fy["STAGE_ID"] == stage.STAGE_ID]
            value = rows["VALUE_BASE"].sum()
            running += value
            label = stage.STAGE_NAME + (f" ({stage.PIPELINE_NAME})" if show_pipeline else "")
            ui.card(col, fmt(value), label,
                    f"{len(rows)} deal(s) · weighted {fmt(rows['WEIGHTED_BASE'].sum())} · "
                    f"cumulative {fmt(running)}",
                    stage_colour=styles[stage.STAGE_ID][0])

if not undated.empty:
    st.caption(
        f"⚠ {len(undated)} open deal(s) worth {fmt(undated['VALUE_BASE'].sum())} have no expected "
        "close date, so they aren't on the chart — listed at the bottom of the page."
    )
missing_fx = deals[deals["FX_RATE"].isna()]["CURRENCY"].dropna().unique()
if len(missing_fx):
    st.warning(f"No conversion rate for {', '.join(missing_fx)} — add one under Currency rates below.")

# ---------------------------------------------------------------- chart
context = (fy, view, measure, won_basis)
if STATE.pick_context != context:
    STATE.picks, STATE.pick_context = set(), context

grid = model.monthly(in_fy, months, measure)
if view == CUMULATIVE:
    grid = model.cumulative(grid)
    chart_target = target.cumsum() if target.notna().any() else target
else:
    chart_target = target
title = f"{view} revenue by deal stage — {model.fy_label(fy)} ({measure_name.lower()}, £)"
CHART_KEY = f"chart-{STATE.version}"


def _on_pick() -> None:
    event = STATE[CHART_KEY]
    points = event["selection"]["points"] if event else []
    new = {
        (p["customdata"][0], int(p["customdata"][1]))
        for p in points
        if p.get("customdata") is not None and len(p["customdata"]) >= 2
    }
    STATE.picks = set() if not new or new == STATE.picks else new


if stages.empty:
    st.info("No deals match the filters.")
else:
    fig = charts.revenue_chart(grid, stages, months, chart_target, STATE.picks, title,
                               show_pipeline=len(chosen_pipes) > 1,
                               styles=charts.stage_styles(model.stage_order(deals_all)))
    with st.container(border=True):
        st.plotly_chart(
            fig, use_container_width=True, key=CHART_KEY, on_select=_on_pick,
            selection_mode="points", theme=None,
            config={"displaylogo": False, "displayModeBar": False},
        )
        st.caption("Click a bar segment to list its deals (shift-click to add more; click it again to clear).")

# ---------------------------------------------------------------- deal list
TABLE_COLUMNS = {
    "TITLE": "Deal", "ORG_NAME": "Organisation", "OWNER_NAME": "Owner", "STAGE_NAME": "Stage",
    "STATUS_LABEL": "Status", "VALUE": "Value", "CURRENCY": "Ccy", "VALUE_BASE": "Value £",
    "PROB": "Probability %", "WEIGHTED_BASE": "Weighted £", "MONTH": "Revenue month",
    "EXPECTED_CLOSE": "Expected close", "WON_DATE": "Won", **{d: d for d in dimensions},
    "SERVICE_LINES": "Service lines", "PIPELINE_NAME": "Pipeline", "URL": "Pipedrive",
}


def deal_table(frame: pd.DataFrame, key: str) -> None:
    show = frame.sort_values(["MONTH", "VALUE_BASE"], ascending=[True, False])
    show = show[[c for c in TABLE_COLUMNS if c in show.columns]].copy()
    for col in show.columns:
        if show[col].map(lambda v: isinstance(v, list)).any():
            show[col] = show[col].map(lambda v: ", ".join(v) if isinstance(v, list) else v)
    show = show.rename(columns=TABLE_COLUMNS)
    st.dataframe(
        show, hide_index=True, use_container_width=True, key=key,
        column_config={
            "Value": st.column_config.NumberColumn(format="%.0f"),
            "Value £": st.column_config.NumberColumn(format="£%.0f"),
            "Weighted £": st.column_config.NumberColumn(format="£%.0f"),
            "Probability %": st.column_config.NumberColumn(format="%.0f%%"),
            "Revenue month": st.column_config.DateColumn(format="MMM YYYY"),
            "Expected close": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Won": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Pipedrive": st.column_config.LinkColumn(display_text="Open"),
        },
    )
    st.caption(f"{len(frame)} deal(s) · {fmt(frame['VALUE_BASE'].sum())} · weighted "
               f"{fmt(frame['WEIGHTED_BASE'].sum())}")


if STATE.picks:
    masks = []
    for month_key, stage_id in STATE.picks:
        month = pd.Timestamp(month_key)
        in_month = in_fy["MONTH"] <= month if view == CUMULATIVE else in_fy["MONTH"] == month
        masks.append(in_month & (in_fy["STAGE_ID"] == stage_id))
    picked = in_fy[pd.concat(masks, axis=1).any(axis=1)]
    names = sorted(
        f"{stages.set_index('STAGE_ID').loc[s, 'STAGE_NAME']} · "
        f"{'to end ' if view == CUMULATIVE else ''}{pd.Timestamp(m):%b-%y}"
        for m, s in STATE.picks
    )
    head = st.columns([5, 1], vertical_alignment="bottom")
    head[0].subheader("Deals in " + "; ".join(names))
    if head[1].button("Clear selection", use_container_width=True):
        STATE.picks = set()
        st.rerun()
    deal_table(picked, "picked")
else:
    st.subheader(f"All deals in {model.fy_label(fy)}")
    deal_table(in_fy, "all")

if not undated.empty:
    with st.expander(f"Open deals with no expected close date ({len(undated)})"):
        deal_table(undated, "undated")

# ---------------------------------------------------------------- targets
st.divider()
with st.expander("Revenue targets — view, edit or upload", expanded=pd.isna(target_total)):
    st.write(
        f"Monthly company revenue targets in £. Edit {model.fy_label(fy)} directly, or upload a CSV/Excel "
        "file with **Month** and **Target** columns (e.g. `Feb-26, 250000`) — uploaded months "
        "replace any existing targets for those months."
    )
    current = pd.DataFrame({"MONTH_START": months, "TARGET": target.values})
    edit_cols = st.columns([2, 3])
    with edit_cols[0]:
        edited = st.data_editor(
            current.assign(Month=[f"{m:%b-%y}" for m in months])[["Month", "TARGET"]],
            column_config={
                "Month": st.column_config.TextColumn(disabled=True),
                "TARGET": st.column_config.NumberColumn("Target £", format="£%.0f", min_value=0),
            },
            hide_index=True, use_container_width=True, key=f"targets-{fy}-{STATE.version}",
        )
        if st.button(f"Save {model.fy_label(fy)} targets", type="primary"):
            data.save_targets(months, pd.DataFrame({"MONTH_START": months, "TARGET": edited["TARGET"].values}),
                              data.current_user())
            _bump()
            st.rerun()
        template = pd.DataFrame({"Month": [f"{m:%b-%y}" for m in months], "Target": target.fillna(0).values})
        st.download_button("Download template (CSV)", template.to_csv(index=False),
                           file_name=f"revenue_targets_{model.fy_label(fy).replace('/', '-')}.csv",
                           mime="text/csv")
    with edit_cols[1]:
        upload = st.file_uploader("Upload targets", type=["csv", "xlsx"], key=f"upload-{STATE.version}")
        if upload is not None:
            try:
                raw = (pd.read_csv(upload) if upload.name.lower().endswith(".csv")
                       else pd.read_excel(io.BytesIO(upload.getvalue())))
                parsed = model.parse_targets(raw)
            except Exception as exc:
                st.error(f"Couldn't read {upload.name}: {exc}")
            else:
                st.dataframe(
                    parsed.assign(Month=parsed["MONTH_START"].dt.strftime("%b-%y"))[["Month", "TARGET"]],
                    column_config={"TARGET": st.column_config.NumberColumn("Target £", format="£%.0f")},
                    hide_index=True,
                )
                if st.button(f"Save {len(parsed)} uploaded month(s)", type="primary"):
                    data.save_targets(list(parsed["MONTH_START"]), parsed, data.current_user())
                    _bump()
                    st.rerun()

with st.expander("Currency rates"):
    st.write(
        "Deal values are converted to £ using Pipedrive's own conversion rates (refreshed with the "
        "deals). Enter an override to use a fixed rate instead; clear it to go back to Pipedrive's."
    )
    rates = fx.rename(columns={"PIPEDRIVE_RATE": "Pipedrive rate", "OVERRIDE_RATE": "Override rate"})
    rates_edit = st.data_editor(
        rates[["CURRENCY", "Pipedrive rate", "Override rate"]],
        column_config={
            "CURRENCY": st.column_config.TextColumn("Currency", disabled=True),
            "Pipedrive rate": st.column_config.NumberColumn(format="%.6f", disabled=True),
            "Override rate": st.column_config.NumberColumn(format="%.6f", min_value=0),
        },
        hide_index=True, key=f"fx-{STATE.version}",
    )
    if st.button("Save currency overrides"):
        data.save_fx_overrides(
            rates_edit.rename(columns={"Override rate": "RATE_TO_BASE"})[["CURRENCY", "RATE_TO_BASE"]],
            data.current_user(),
        )
        _bump()
        st.rerun()
