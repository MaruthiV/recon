-- local dev roles only, databases and grants come from `make db` (recon_storage.bootstrap)
CREATE ROLE recon_owner LOGIN PASSWORD 'recon_owner';
CREATE ROLE recon_app LOGIN PASSWORD 'recon_app';
CREATE ROLE recon_mcp_read LOGIN PASSWORD 'recon_mcp_read';
CREATE ROLE sim_app LOGIN PASSWORD 'sim_app';
CREATE ROLE recon_eval LOGIN PASSWORD 'recon_eval';
