"""Stacked revenue chart: one bar segment per (month, stage), target line on top."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from prd.model import fmt_money

FONT = "Segoe UI, DIN, Helvetica, Arial, sans-serif"
# The nine official Strata colours, most certain stage (bottom of the stack) first.
STRATA = ["#265254", "#0A7361", "#699996", "#4A8AA8", "#BAA12E", "#FAB524", "#F28A40",
          "#CC4A38", "#DEA8CC"]
TARGET_COLOUR = "#000000"
DIM = 0.25
PATTERNS = ["", "/", ".", "x", "-", "|", "+", "\\"]


def _rgba(colour: str, alpha: float) -> str:
    r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


def _ink(colour: str) -> str:
    r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return "#000000" if (r * 299 + g * 587 + b * 114) / 1000 >= 150 else "#FFFFFF"


def stage_styles(stages: pd.DataFrame) -> dict:
    """{stage id: (colour, pattern)}; colours restart per pipeline, which then
    gets its own hatch pattern."""
    styles, pipelines = {}, list(dict.fromkeys(stages["PIPELINE_ID"]))
    for pipe_no, pipe in enumerate(pipelines):
        rows = stages[stages["PIPELINE_ID"] == pipe]
        for i, stage_id in enumerate(rows["STAGE_ID"]):
            styles[stage_id] = (STRATA[i % len(STRATA)], PATTERNS[pipe_no % len(PATTERNS)])
    return styles


def revenue_chart(
    grid: pd.DataFrame,
    stages: pd.DataFrame,
    months: list[pd.Timestamp],
    target: pd.Series,
    picked: set[tuple[str, int]],
    title: str,
    show_pipeline: bool,
    styles: dict | None = None,
) -> go.Figure:
    labels = [f"{m:%b-%y}" for m in months]
    keys = [m.strftime("%Y-%m-%d") for m in months]
    styles = styles or stage_styles(stages)
    fig = go.Figure()
    for stage in stages.itertuples(index=False):
        rows = grid[grid["STAGE_ID"] == stage.STAGE_ID].set_index("MONTH").reindex(months)
        values = rows["VALUE"].fillna(0.0).tolist()
        deals = rows["DEALS"].fillna(0).astype(int).tolist()
        if not any(values) and not any(deals):
            continue
        colour, pattern = styles[stage.STAGE_ID]
        name = stage.STAGE_NAME if not show_pipeline else f"{stage.STAGE_NAME} ({stage.PIPELINE_NAME})"
        fill = [
            colour if not picked or (k, int(stage.STAGE_ID)) in picked else _rgba(colour, DIM)
            for k in keys
        ]
        fig.add_bar(
            x=labels, y=values, name=name,
            marker={"color": fill, "line": {"width": 0},
                    "pattern": {"shape": pattern, "fgcolor": "#FFFFFF", "size": 6}},
            customdata=[[k, int(stage.STAGE_ID), d, fmt_money(v)] for k, d, v in zip(keys, deals, values)],
            hovertemplate=f"<b>{name}</b><br>%{{x}}<br>%{{customdata[3]}} · %{{customdata[2]}} deal(s)"
            "<extra></extra>",
            hoverlabel={"bgcolor": colour, "font": {"color": _ink(colour), "family": FONT}},
        )
    totals = grid.groupby("MONTH")["VALUE"].sum().reindex(months).fillna(0.0)
    fig.add_scatter(
        x=labels, y=totals.tolist(), mode="text", text=[fmt_money(v) if v else "" for v in totals],
        textposition="top center", textfont={"family": FONT, "size": 11, "color": "#000000"},
        hoverinfo="skip", showlegend=False, name="Total",
    )
    if target.notna().any():
        fig.add_scatter(
            x=labels, y=target.tolist(), name="Target", mode="lines+markers",
            line={"color": TARGET_COLOUR, "width": 2.5, "dash": "dash"},
            marker={"size": 7, "color": TARGET_COLOUR},
            text=[fmt_money(v) for v in target],
            hovertemplate="<b>Target</b><br>%{x}<br>%{text}<extra></extra>",
            hoverlabel={"bgcolor": "#FFFFFF", "font": {"color": "#000000", "family": FONT}},
        )
    top = max([totals.max() or 0, target.max() if target.notna().any() else 0, 1])
    fig.update_layout(
        barmode="stack", height=520, title={"text": title, "font": {"family": FONT, "size": 16}},
        font={"family": FONT, "color": "#000000"}, plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
        margin={"l": 70, "r": 10, "t": 50, "b": 10},
        legend={"orientation": "h", "yanchor": "top", "y": -0.08, "x": 0, "traceorder": "normal"},
        yaxis={"gridcolor": "#E6E6E6", "tickprefix": "£", "tickformat": "~s", "automargin": True, "range": [0, top * 1.12]},
        xaxis={"type": "category", "categoryorder": "array", "categoryarray": labels},
        clickmode="event+select", dragmode=False, bargap=0.25,
    )
    return fig
