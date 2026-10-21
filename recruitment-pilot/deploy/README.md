# Botkeep online pilot

## Release

Build frontend, then run `python deploy/package_release.py`. The whitelist ZIP is
written under ignored `runtime/releases`. Its manifest records the base commit,
whether the working tree is modified, and SHA256 for every packaged file. Do not
describe a modified-tree release as an exact commit release. No local uploads,
database dump, `.env`, credentials, or runtime directory belong in this ZIP.

## Infrastructure gate

Require verified PostgreSQL TLS, persistent application files and HTTPS.
For the private two-user public-data pilot, the user approved temporarily using
Botkeep's supplied database-owner login for both migrations and application
runtime. This login can change schema; running migrations separately does not
remove that privilege. Botkeep support confirmed on 2026-10-14 that split users
and application-specific private networking/allowlists are not currently offered.
Do not represent this setup as least privilege or private-only networking.
Keep the support request for future split roles. No CV pool or private notes.

## Environment and startup

- `PILOT_MODE=ONLINE_PILOT`
- `PILOT_ORIGIN=https://ssresq.bot-keep.xyz`
- `PILOT_RELEASE`: release ID from the manifest
- `PILOT_RUNTIME_DIR=/home/container/runtime` (verify persistence before use)
- `DATABASE_URL`: supplied login's SQLAlchemy psycopg URL, `sslmode=verify-full`
  and the uploaded CA certificate path
- `PILOT_USERS_JSON`: exactly `tri/admin` and `dat/recruiter`, scrypt hashes only
- Provider API keys in Environment; never in ZIP, commands or console logs
- `SERVER_PORT`: Botkeep-assigned value; do not hardcode the localhost port

Install `requirements-lock.txt` in Python 3.11 and check dependency compatibility
on host. Run `python deploy/migrate.py` with `MIGRATION_DATABASE_URL` in a
separate maintenance process; remove that variable before starting the service.
Start with `python deploy/start_online.py`. One Uvicorn process starts one
business worker. Proxy headers are disabled; require the gateway to preserve
the HTTPS origin's Host header and verify authenticated CSRF writes in browser.

When separate roles become available, migrate to a runtime login with only
schema USAGE, table DML and sequence USAGE/SELECT. Keep schema ownership and
CREATE with the migration login. Until then, record the single-login exception
in the handover and never grant SUPERUSER/CREATEROLE to the application.

## Acceptance and rollback

Check health/readiness, authentication, direct exports, role restrictions, three
fixture JDs, version conflicts, no-provider read actions, simultaneous sessions,
restart and uncertain-request handling. Fixtures do not prove live provider
success. Paid calls require explicit user confirmation.

Before migrations, save a logical DB backup and a separate uploads archive off
host. Restore on an isolated DB and compare counts/hashes. Use a maintenance
window and keep the preceding release and migration-compatible backup. Do not
blindly replay interrupted provider requests. Do not hand over the HTTPS URL as
ready until full browser acceptance and restore have passed.
