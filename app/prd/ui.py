"""Page furniture: CSS, logo and summary cards."""

from __future__ import annotations

import base64
from pathlib import Path

import streamlit as st

ASSETS = Path(__file__).resolve().parent.parent / "assets"
LOGO = ASSETS / "strata_logo.png"

CSS = """
<style>
.block-container {padding-top: 2.6rem; padding-bottom: 1rem; max-width: 100%;}
.prd-card {border: 1px solid #E1DFDD; border-radius: 8px; padding: 8px 14px; min-height: 86px;
  display: flex; flex-direction: column; justify-content: center; background: #FFFFFF;
  margin-bottom: 8px;}
.prd-card .value {font-family: 'Segoe UI Semibold', 'Segoe UI', sans-serif; font-size: 22px;
  color: #000000; line-height: 1.2;}
.prd-card .label {font-family: 'Segoe UI', sans-serif; font-size: 12px; color: #000000;}
.prd-card .sub {font-family: 'Segoe UI', sans-serif; font-size: 11px; color: #605E5C;}
.prd-card.good .value {color: #0A7361;}
.prd-card.bad .value {color: #CC4A38;}
.prd-stage {border-left: 6px solid var(--stage);}
.prd-logo {display: flex; justify-content: flex-end; align-items: center; height: 64px;}
.prd-logo img {max-height: 46px; max-width: 100%;}
.prd-title {font-family: 'Segoe UI Semibold', 'Segoe UI', sans-serif; font-size: 28px;
  color: #000000; margin: 0; line-height: 64px;}
</style>
"""


def page_setup() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def title(container, text: str) -> None:
    container.markdown(f'<div class="prd-title">{text}</div>', unsafe_allow_html=True)


def logo(container) -> None:
    if LOGO.exists():
        uri = "data:image/png;base64," + base64.b64encode(LOGO.read_bytes()).decode()
        container.markdown(f'<div class="prd-logo"><img src="{uri}" alt="Strata"></div>',
                           unsafe_allow_html=True)


def card(container, value: str, label: str, sub: str = "", tone: str = "", stage_colour: str = "") -> None:
    cls = f"prd-card {tone}" + (" prd-stage" if stage_colour else "")
    style = f' style="--stage: {stage_colour}"' if stage_colour else ""
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    container.markdown(
        f'<div class="{cls}"{style}><div class="value">{value}</div>'
        f'<div class="label">{label}</div>{sub_html}</div>',
        unsafe_allow_html=True,
    )
