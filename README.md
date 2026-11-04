# Booru Downloader

Source for the currently deployed Booru collection and download app. The Python application and `static/` files were copied from the running Docker image on November 4, 2026. The repository includes a standalone Docker build and a Compose configuration.

## Requirements

- Docker Engine with Compose
- MySQL 8 with the `booru` database reachable by hostname over TLS
- A writable downloads directory mounted on the Docker host
- A MySQL password file named `mysql-password` and CA certificate named `mysql-ca.pem` in a host secrets directory
- A writable host directory for saved site credentials

The application stores searches, posts, jobs, and file records in MySQL. It stores site credentials as files in the credentials mount. Downloaded media stays in the downloads mount. None of those values are included in this repository.

## Deploy with the existing database

1. Copy `.env.example` to `.env` on the Docker host and set each host path. `MYSQL_HOST` must be the MySQL server's hostname.
2. Point `BOORU_CREDENTIALS_HOST_PATH` at the **existing** saved-credentials directory if retaining current site profiles. Their database records refer to files in that directory. The live deployment currently mounts `/opt/yandere-web/backups/relational-20261028_185449/credentials` at `/run/booru-credentials`; copying that directory to a new host path also works if permissions are preserved.
3. To reuse the current `/data` volume, set `DATA_VOLUME_NAME=yandere-web_yandere-data`. Set `DOWNLOADS_HOST_PATH=/mnt/nas-data/wallpapers` to retain the current `/downloads` layout.
4. Stop the existing container or stack so port 9190 is available. Keep its MySQL database, credentials directory, downloads mount, and `/data` volume.
5. From this repository on the Docker host, run:

   ```sh
   docker compose up -d --build
   docker compose ps
   ```

The app listens on port 9190 by default. The container health check calls `/api/health`.

## Deploy with a new empty database

The included `schema.sql` is the table structure exported from the live MySQL database. It contains no application rows or credentials. It ends with the schema version marker required at app startup. It targets MySQL 8's `utf8mb4_0900_bin` collation.

Create an empty `booru` database, import `schema.sql` into it, and grant the app user read/write access to that database. For example, from a MySQL client with administrative access:

```sql
CREATE DATABASE booru CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
CREATE USER 'booru_app'@'%' IDENTIFIED BY 'choose-a-password' REQUIRE SSL;
GRANT SELECT, INSERT, UPDATE, DELETE ON booru.* TO 'booru_app'@'%';
```

Then import the schema once:

```sh
mysql -h mysql.thelinclan.com -u root -p booru < schema.sql
```

Put the app user's password alone in the host `mysql-password` file, put the issuing CA certificate in `mysql-ca.pem`, and set the paths in `.env`. Create the download and credential directories before `docker compose up -d --build`. Add site profiles through the app after it starts.

**Do not import `schema.sql` into an existing populated database.** It contains `CREATE TABLE` statements.

## Repository contents

- `app.py`, adapter and storage modules: current application code
- `static/`: current browser interface
- `schema.sql`: current table definitions for a new database
- `Dockerfile`, `compose.yaml`, `.env.example`: container build and deployment
- `test_relational.py`: relational storage tests from the live image

`.env`, credentials, secrets, downloads, and database dumps are ignored by Git and excluded from the image build context.
