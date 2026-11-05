# Project status

Snapshot: November 4, 2026 (America/Denver). Live state can change; recheck before acting.

## Working and verified

- Production runs image `yandere-web:3.0.11-site-media-auth` on `docker.thelinclan.com`. Docker reported it healthy and `/api/health` returned HTTP 200 during this snapshot.
- The repository's key application files were copied from that running image and their SHA-256 hashes matched. The standalone `Dockerfile` built successfully, and `compose.yaml` passed `docker compose config` validation. Production still uses its existing stack.
- MySQL `booru` on `mysql.thelinclan.com` has 31 active site profiles, 3 archived profiles, and 478 active searches. No searches remain marked deleted, and the purge queue has no pending markers. The previously requested purge is complete at the database search-record level.
- Site profiles, searches, normalized post records, saved-tag browsing, the download queue, and persisted file associations are present in the running app. Credentials live in a host-mounted directory rather than database JSON or this repository.
- The Sankaku expired-link fix returned a fresh signed URL for one saved post, and its normal retry saved that file. The ATF credential fix returned image data and its normal retry saved one original file. These are verified examples, not a check of every post.
- The UI exposes API and file rates as requests per minute. The backend converts them to spacing in seconds and also responds to site-provided rate-limit signals.
- Saved-result filtering supports id:<external-post-id> and job:<download-job-number> prefixes. Both lookup modes stay within the selected collection and return matching posts in the regular card grid; unprefixed input goes directly through tag search. Python compilation, JavaScript syntax check, Compose validation, and a clean Docker build passed. The user confirmed the lookups work.
- Sankaku empty-page exclusions were applied on November 4, 2026. The production `yandere-web` service now runs `ghcr.io/entheosbardou/booru-downloader:latest` (image config `sha256:f7433f231db2abe0f324186734b9eb886db27da44a9a2c22fe436f2702634140`); the running `app.py`, `relational.py`, and failure-export UI hashes matched this repository. A targeted before-image is stored at `/opt/yandere-web/backups/sankaku-empty-redirect-before-20261104.json.gz` (SHA-256 `fb9523e027b7b94a734329c259fdba57aef12bbd44c183238ff0f07e5f42bafa`, mode 600). Of 285 exported Sankaku failures, 282 redirected to `en/posts/show_empty`; those 282 posts are retained with `availability_status='empty_redirect'`, excluded from saved results, and skipped by future crawls. Direct checks of both deployed save paths skipped a marked post without contacting Sankaku. Their 282 failed jobs are cleared. Search 451 decreased from 2,264 to 1,982 results. Three other destinations were left untouched; no physical downloads were associated with the 282 posts. The full-failure export endpoint returned all 237 remaining failures, including `site_api`; the app health endpoint returned HTTP 200 after deployment. Python compilation, JavaScript syntax check, Compose configuration validation, and a clean image build succeeded.

## Recorded failures and risks

- The database retains 233 jobs with `error` status. They are historical records, not 233 currently running tasks. The largest groups are Danbooru downloads (86, often no original URL), shima downloads (60, often no original URL), and Rule34.xxx downloads (60, checksum or size failures). Five Sankaku crawl jobs also retain HTTP 401 errors. Inspect a job before retrying it.
- Queue dispatcher `GET_LOCK` timeouts occurred in logs on November 3. The app currently uses one named dispatch lock with a 30-second wait. No such error appeared in the last 24 hours of logs checked for this snapshot, but the underlying contention risk remains.
- Some remote posts do not expose an original URL or return a file matching their stated checksum or size. A successful site connection does not guarantee access to every original file.

## Unfinished or not verified

- The repository Compose stack has **not** been used to replace the production stack. Its Docker build and Compose syntax were verified; a full fresh deployment and cutover were not performed.
- `schema.sql` is a structure-only export for a new empty MySQL 8 database. Its fresh-database import and first-run workflow were not exercised. Never apply it to the populated production database.
- `test_relational.py` requires a synthetic fixture database and was not run as part of the repository copy. The source modules compiled successfully in the built image.
- The `.env.example` credential path is an example. Before a production cutover, point `BOORU_CREDENTIALS_HOST_PATH` at the existing credential directory or move that directory while preserving its contents and permissions. Reuse the current `/data` volume and downloads root if retaining live state.

## Deployment pointers

See `README.md` for Compose setup. Keep the MySQL password file, TLS CA, credentials directory, `/data` volume, and `/downloads` mount persistent. The production database is `booru`; the running container and this repository should not both start workers against it.
