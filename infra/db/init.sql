-- local dev only, cloud uses secret manager
CREATE ROLE recon_owner LOGIN PASSWORD 'recon_owner';
CREATE ROLE recon_app LOGIN PASSWORD 'recon_app';
CREATE ROLE recon_mcp_read LOGIN PASSWORD 'recon_mcp_read';
CREATE ROLE sim_app LOGIN PASSWORD 'sim_app';
CREATE ROLE recon_eval LOGIN PASSWORD 'recon_eval';

CREATE DATABASE recon OWNER recon_owner;
CREATE DATABASE provider_sim OWNER sim_app;
CREATE DATABASE recon_eval OWNER recon_eval;

REVOKE ALL ON DATABASE recon, provider_sim, recon_eval FROM PUBLIC;
GRANT CONNECT ON DATABASE recon TO recon_app, recon_mcp_read, recon_eval;
GRANT CONNECT ON DATABASE provider_sim TO recon_eval;

\connect recon
CREATE EXTENSION IF NOT EXISTS vector;
GRANT USAGE ON SCHEMA public TO recon_app, recon_mcp_read, recon_eval;
ALTER DEFAULT PRIVILEGES FOR ROLE recon_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO recon_app;
ALTER DEFAULT PRIVILEGES FOR ROLE recon_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO recon_app;
-- narrowed per table in migrations once tools exist
ALTER DEFAULT PRIVILEGES FOR ROLE recon_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO recon_mcp_read, recon_eval;
-- adk creates its own session tables here
CREATE SCHEMA adk AUTHORIZATION recon_app;

\connect provider_sim
GRANT USAGE ON SCHEMA public TO recon_eval;
ALTER DEFAULT PRIVILEGES FOR ROLE sim_app IN SCHEMA public
    GRANT SELECT ON TABLES TO recon_eval;
