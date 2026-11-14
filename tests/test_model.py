import json

import pandas as pd
import pytest

from pipedrive_sync import sync
from prd import model

FIELDS = [
    {"key": "label", "name": "Label", "field_type": "set",
     "options": [{"id": 15, "label": "Implementation - Aladdin"}, {"id": 17, "label": "Compliance"}]},
    {"key": "a" * 40, "name": "Client Region", "field_type": "enum",
     "options": [{"id": 38, "label": "APAC"}, {"id": 39, "label": "EMEA"}]},
    {"key": "b" * 40, "name": "Notes", "field_type": "varchar", "options": None},
]


def _deal(i, status, stage, value, currency="GBP", ecd=None, won=None, region=None, label=None):
    return {
        "id": i, "title": f"Deal {i}", "status": status, "pipeline_id": 1, "stage_id": stage,
        "value": value, "currency": currency, "expected_close_date": ecd, "won_time": won,
        "local_won_date": won[:10] if won else None, "probability": None,
        "user_id": {"id": 7, "name": "Greg"}, "owner_name": "Greg",
        "org_id": {"value": 3, "name": "Aegon"}, "org_name": "Aegon",
        "label": label, "a" * 40: region, "b" * 40: "free text",
    }


@pytest.fixture
def fetched():
    return {
        "deals": [
            _deal(1, "open", 2, 100_000, ecd="2026-03-15", region=39, label="15,17"),
            _deal(2, "won", 5, 50_000, ecd="2026-01-20", won="2026-02-03 10:00:00", region=38),
            _deal(3, "open", 1, 200_000, currency="USD", ecd="2027-01-31"),
            _deal(4, "open", 1, 10_000),  # no expected close date
            _deal(5, "lost", 2, 99_000, ecd="2026-05-01"),
        ],
        "stages": [
            {"id": 1, "pipeline_id": 1, "order_nr": 0, "name": "20% Lead ", "deal_probability": 20},
            {"id": 2, "pipeline_id": 1, "order_nr": 1, "name": "50% Proposal", "deal_probability": 50},
            {"id": 5, "pipeline_id": 1, "order_nr": 2, "name": "100% Signed", "deal_probability": 100},
        ],
        "pipelines": [{"id": 1, "name": "Sales Pipeline", "order_nr": 1, "active": True}],
        "users": [],
        "deal_fields": FIELDS,
        "summary": {"values_total": {"USD": {"value": 200_000, "value_converted": 150_000}}},
        "me": {"default_currency": "GBP"},
    }


@pytest.fixture
def deals(fetched):
    fields = sync.field_rows(fetched)
    dims = model.custom_dimensions(fields)
    fx = model.fx_table(sync.fx_rows(fetched), pd.DataFrame(columns=["CURRENCY", "RATE_TO_BASE"]))
    built = model.build_deals(sync.deal_rows(fetched), sync.stage_rows(fetched),
                              sync.pipeline_rows(fetched), fx, dims)
    return model.with_revenue_month(built)


def test_custom_fields_and_labels_decoded(fetched):
    rows = sync.deal_rows(fetched)
    assert json.loads(rows.loc[0, "CUSTOM_FIELDS"]) == {"Client Region": "EMEA", "Notes": "free text"}
    assert json.loads(rows.loc[0, "LABELS"]) == ["Implementation - Aladdin", "Compliance"]
    assert json.loads(rows.loc[3, "LABELS"]) == []


def test_only_option_fields_become_filters(fetched):
    assert model.custom_dimensions(sync.field_rows(fetched)) == ["Client Region"]


def test_fx_from_pipedrive_summary(fetched):
    fx = sync.fx_rows(fetched).set_index("CURRENCY")["RATE_TO_BASE"]
    assert fx["GBP"] == 1.0 and fx["USD"] == pytest.approx(0.75)


def test_fx_override_wins():
    rates = pd.DataFrame({"CURRENCY": ["GBP", "USD"], "RATE_TO_BASE": [1.0, 0.75]})
    over = pd.DataFrame({"CURRENCY": ["USD"], "RATE_TO_BASE": [0.8]})
    assert model.fx_table(rates, over).set_index("CURRENCY").loc["USD", "RATE"] == 0.8


def test_values_probability_and_weighting(deals):
    d = deals.set_index("ID")
    assert d.loc[3, "VALUE_BASE"] == pytest.approx(150_000)
    assert d.loc[1, "WEIGHTED_BASE"] == pytest.approx(50_000)
    assert d.loc[2, "PROB"] == 100 and d.loc[5, "PROB"] == 0
    assert d.loc[1, "Client Region"] == "EMEA"
    assert d.loc[1, "STAGE_NAME"] == "50% Proposal"


def test_financial_year_runs_feb_to_jan(deals):
    assert model.fy_start_year("2026-02-01") == 2026
    assert model.fy_start_year("2027-01-31") == 2026
    assert model.fy_start_year("2026-01-31") == 2025
    assert model.fy_label(2026) == "FY2026/27"
    months = model.fy_months(2026)
    assert months[0] == pd.Timestamp("2026-02-01") and months[-1] == pd.Timestamp("2027-01-01")
    d = deals.set_index("ID")
    assert d.loc[3, "FY"] == 2026  # Jan-27 belongs to FY2026/27
    assert pd.isna(d.loc[4, "MONTH"])


def test_won_deals_use_won_date_or_expected_close(deals):
    d = deals.set_index("ID")
    assert d.loc[2, "MONTH"] == pd.Timestamp("2026-02-01") and d.loc[2, "FY"] == 2026
    by_expected = model.with_revenue_month(deals, model.EXPECTED_DATE).set_index("ID")
    assert by_expected.loc[2, "MONTH"] == pd.Timestamp("2026-01-01")
    assert by_expected.loc[2, "FY"] == 2025


def test_monthly_and_cumulative(deals):
    months = model.fy_months(2026)
    in_fy = deals[(deals["FY"] == 2026) & (deals["STATUS"] != "lost")]
    grid = model.monthly(in_fy, months, "VALUE_BASE")
    assert len(grid) == 12 * in_fy["STAGE_ID"].nunique()
    assert grid["VALUE"].sum() == pytest.approx(in_fy["VALUE_BASE"].sum())
    cum = model.cumulative(grid)
    jan = cum[cum["MONTH"] == months[-1]]
    assert jan["VALUE"].sum() == pytest.approx(in_fy["VALUE_BASE"].sum())


def test_stage_order_most_certain_first(deals):
    assert list(model.stage_order(deals)["STAGE_ID"]) == [5, 2, 1]


@pytest.mark.parametrize("month", ["2026-02", "Feb-26", "Feb 2026", "01/02/2026", "February 2026",
                                   pd.Timestamp("2026-02-14")])
def test_parse_targets_long(month):
    out = model.parse_targets(pd.DataFrame({"Month": [month], "Target": ["£250,000"]}))
    assert out.iloc[0]["MONTH_START"] == pd.Timestamp("2026-02-01")
    assert out.iloc[0]["TARGET"] == 250_000


def test_parse_targets_wide_and_suffix():
    out = model.parse_targets(pd.DataFrame({"Feb-26": ["1.5m"], "Mar-26": ["200k"]}))
    assert list(out["TARGET"]) == [1_500_000, 200_000]


def test_parse_targets_rejects_bad_month():
    with pytest.raises(ValueError):
        model.parse_targets(pd.DataFrame({"Month": ["Smarch"], "Target": [1]}))


def test_target_series_and_money_format():
    months = model.fy_months(2026)
    t = model.target_series(pd.DataFrame({"MONTH_START": ["2026-03-01"], "TARGET": [5]}), months)
    assert t.iloc[1] == 5 and pd.isna(t.iloc[0])
    assert model.fmt_money(1_234_567) == "£1.23M"
    assert model.fmt_money(-950) == "-£950"
