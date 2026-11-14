-- One-off admin setup for the Pipedrive revenue dashboard.
-- Run once in a Snowsight worksheet as ACCOUNTADMIN (select all, Run).
USE ROLE ACCOUNTADMIN;

-- New database for the Pipedrive data, targets, refresh task and Streamlit app.
CREATE DATABASE IF NOT EXISTS PIPEDRIVE COMMENT = 'Pipedrive deals + revenue targets';
CREATE SCHEMA IF NOT EXISTS PIPEDRIVE.INTEGRATION;  -- admin-owned: holds only the network rule; the app objects live in PIPEDRIVE.SYNC

-- Let Snowflake call the Pipedrive API (outbound HTTPS) from a Python stored procedure.
CREATE OR REPLACE NETWORK RULE PIPEDRIVE.INTEGRATION.PIPEDRIVE_API_RULE
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = ('api.pipedrive.com:443');
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION PIPEDRIVE_API_INTEGRATION
  ALLOWED_NETWORK_RULES = (PIPEDRIVE.INTEGRATION.PIPEDRIVE_API_RULE)
  ALLOWED_AUTHENTICATION_SECRETS = ALL
  ENABLED = TRUE
  COMMENT = 'Hourly Pipedrive deal sync';

-- Hand the database to the app role (ALPHA_APP), keep admin access.
GRANT OWNERSHIP ON DATABASE PIPEDRIVE TO ROLE ALPHA_APP_READER COPY CURRENT GRANTS;
GRANT USAGE ON DATABASE PIPEDRIVE TO ROLE ACCOUNTADMIN;
GRANT USAGE ON INTEGRATION PIPEDRIVE_API_INTEGRATION TO ROLE ALPHA_APP_READER;

-- Allow the app role to run the hourly (Mon-Fri) refresh task.
GRANT EXECUTE TASK ON ACCOUNT TO ROLE ALPHA_APP_READER;
