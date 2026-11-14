# Pipedrive revenue dashboard

Pipedrive deals → Snowflake `PIPEDRIVE` database → Streamlit in Snowflake app
`PIPEDRIVE.APP.PIPEDRIVE_REVENUE_DASHBOARD`: monthly / cumulative revenue stacked by deal stage
for the Strata financial year (1 Feb – 31 Jan), with a target line, filters and click-through to deals.

## Snowflake layout
| Object | Purpose |
| --- | --- |
| `PIPEDRIVE.INTEGRATION` | Admin-owned; network rule for `api.pipedrive.com` (see `snowflake/00_admin_setup.sql`) |
| `PIPEDRIVE.SYNC` | `SYNC_PIPEDRIVE()` procedure, `SYNC_PIPEDRIVE_HOURLY` task (hourly Mon–Fri, Europe/London), token secret, `SYNC_LOG` |
| `PIPEDRIVE.RAW` | Full refresh of deals, stages, pipelines, users, deal fields, FX rates each sync |
| `PIPEDRIVE.ANALYTICS.DEALS` | Typed view: GBP values, weighted values, regions, service lines, Pipedrive links |
| `PIPEDRIVE.APP` | `REVENUE_TARGETS`, `FX_OVERRIDES` (edited in the app) and the Streamlit app |

## Setup / deploy
1. Once, as ACCOUNTADMIN: `snowflake/00_admin_setup.sql`.
2. Copy `app/.streamlit/secrets.toml.example` to `app/.streamlit/secrets.toml`.
3. `PIPEDRIVE_API_TOKEN=... python scripts/deploy.py --sync` (`--app-only` re-uploads just the app).

## Local development
- `pytest` runs the model tests.
- `PRD_OFFLINE_FETCH=data/fetch.json streamlit run app/streamlit_app.py` runs the app with no Snowflake,
  from a JSON dump of `pipedrive_sync.sync.fetch(token)` (`data/` is git-ignored).
