# Codex instructions for Booru Downloader

## Scope

- These instructions apply to entheosbardou/booru-downloader only. Keep changes focused on the requested application work.
- Do not deploy, restart production services, push to GitHub, or enable automatic production deployment without user authorization for that action. Editing source or building an image does not grant it.

## Source and deployment

- Treat this repository as the editable source. Check the running image and repository diff before saying a change is deployed. The production container currently runs on `docker.thelinclan.com`; MySQL runs on `mysql.thelinclan.com`. Use the configured SSH aliases `linux-docker` and `linux-mysql` and prefer hostnames over IP addresses.
- The production stack is at `/volume1/docker/portainer/compose/226/docker-compose.yml`. The repository's `compose.yaml` builds a standalone image, but has not replaced that stack. Never run both worker-bearing containers against the same `booru` database at once.
- Keep MySQL passwords, CA certificates, site cookies, API keys, downloaded files, and the credential directory outside Git and Docker build context. Do not print secret values in commands, logs, tests, or responses. Existing profile records refer to files in the credential mount.
- Preserve the host NAS root `/mnt/nas-data/wallpapers` mounted as `/downloads`; stored file paths are relative to that root.
- When source is updated, update the files in the folder containing this AGENTS.md file.

## Data rules

- Do not hard-code individual searches, posts, tag names or IDs, or similar site records into application behavior. Resolve them through the source site or use user-provided values.
- MySQL stores values in typed columns and relationship tables. Do not add JSON blobs, executable instructions, triggers, routines, views, or generated columns to hold application behavior. The UI may accept JSON input for mappings, but persistence splits it into relational rows.
- Before a production schema change or data migration, show the exact tables, columns, indexes, relationships, and data changes, plus backup, rollback, and verification steps to the user and wait for explicit approval. Do not import `schema.sql` into an existing populated database.
- Removing a site archives its identity for provenance and removes runtime state, source options, and credentials. Searches, posts, and file records retain their source identity. Archived sites must not start new searches.
- Deleting a search or collection permanently purges its database records. Preserve posts and database file records still linked to another search. Never delete actual downloaded files unless the user specifically requests that.
- Keep database transactions small. Avoid full-table updates and long holds of the dispatcher lock. The purge worker processes bounded batches; do not increase batch sizes without measuring lock behavior.
- Rate controls are shown as requests per minute. The app stores their request spacing in seconds. Preserve that conversion and server-directed cooldown behavior. Send saved credentials only to the configured API host and strip them on cross-host redirects.


## No monitoring unless requested

- Do not monitor, poll, tail logs continuously, create recurring checks or alerts, schedule follow-ups, or produce background status reports unless the user explicitly requests monitoring. Starting a job, making a fix, or deploying a worker does not authorize monitoring it.
- A bounded, one-time check needed to verify an authorized change is allowed. It must not become a retry loop or ongoing observation. For a long-running job, report that it started and stop observing; inspect its progress or completion only when the user asks.
- This rule governs agent observation and automation. It does not authorize disabling the application's existing workers, queues, or health checks.

## Verification

- For code changes, run Python compilation, `node --check static/app.js`, `docker compose --env-file .env.example config --quiet`, and a clean Docker build when applicable. Check the container health endpoint after a deployment.
- `test_relational.py` expects synthetic fixture data and a configured MySQL test environment. Do not run it against production.
- Test a relevant search, preview, or download path when fixing that behavior. Distinguish one verified post from site-wide success. Report remaining errors and work that was not tested.
- A successful build does not mean the repository Compose stack has been deployed. Record the image tag and verification evidence in `PROJECT_STATUS.md` when deployment state changes.
- Distinguish source edits, successful builds, deployed changes, and completed jobs. Report what was verified, remaining errors, and material limits concisely. Treat deployment pointers as recorded context; verify them before an authorized deployment, without starting ongoing monitoring.
