# Migrating Zabbix from MariaDB to PostgreSQL + TimescaleDB

Zabbix history is append-only time-series data stored in ordinary relational
tables. On MySQL/MariaDB that means it grows without bound until housekeeping
deletes rows, and housekeeping on a large `history` table is slow and lock-heavy.

TimescaleDB turns those tables into hypertables partitioned by time. Deleting
old data becomes dropping a chunk instead of a mass `DELETE`, and compression on
older chunks cuts the on-disk size dramatically.

This is a real migration performed on a production Zabbix 7.4 server: **39.7M
history rows moved in 13 minutes 39 seconds**, with the old database kept
running as a fallback until the new one was proven.

> **Take a snapshot first.** This procedure changes the authoritative data store
> for your entire monitoring system. On Proxmox:
> `qm snapshot <vmid> pre-pg-migration`

---

## 1. Capacity

History and trends are the bulk of the database. Check before you start:

```sql
-- MariaDB
SELECT table_name, ROUND(data_length/1024/1024) AS mb
FROM information_schema.tables
WHERE table_schema='zabbix' ORDER BY data_length DESC LIMIT 10;
```

You need room for both databases at once. Growing the disk first is much less
stressful than running out halfway through a backfill:

```bash
growpart /dev/sda 1 && resize2fs /dev/sda1
```

## 2. Install PostgreSQL and TimescaleDB

```bash
apt-get install -y postgresql-16 postgresql-contrib
# Add the TimescaleDB repository for your distribution, then:
apt-get install -y timescaledb-2-postgresql-16
timescaledb-tune --quiet --yes
systemctl restart postgresql
```

> **Version compatibility is a real constraint.** Zabbix 7.4 officially supports
> TimescaleDB up to **2.28**. If your repository installs 2.29 or newer the
> server refuses to start with an unsupported-version error. Either pin 2.28.x,
> or set `AllowUnsupportedDBVersions=1` in `zabbix_server.conf`. 2.29 works
> fine in practice, but know which choice you made.

## 3. Create the role and database

```bash
sudo -u postgres createuser --pwprompt zabbix
sudo -u postgres createdb -O zabbix zabbix
sudo -u postgres psql -d zabbix -c "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"
```

Store the password somewhere real (a secrets manager), not in a shell history.

## 4. Load the native schema

Load Zabbix's own PostgreSQL schema. **Do not** try to translate the MySQL
schema — types, sequences, and constraints differ.

```bash
zcat /usr/share/zabbix/sql-scripts/postgresql/server.sql.gz | \
    sudo -u zabbix psql zabbix
```

Verify: 207 tables, and `dbversion` reporting your Zabbix version.

## 5. Migrate configuration data

Use [pgloader](https://pgloader.io/) in **data-only** mode so it fills the
native schema rather than inventing its own.

```
LOAD DATABASE
    FROM mysql://zabbix:PASSWORD@localhost/zabbix
    INTO postgresql://zabbix:PASSWORD@localhost/zabbix
WITH data only, disable triggers, preserve index names
SET work_mem to '256MB', maintenance_work_mem to '1GB'
ALTER SCHEMA 'zabbix' RENAME TO 'public'
EXCLUDING TABLE NAMES MATCHING 'history', 'history_uint', 'history_str',
                               'history_text', 'history_log',
                               'trends', 'trends_uint', 'auditlog';
```

> **The `ALTER SCHEMA 'zabbix' RENAME TO 'public'` line is not optional.**
> Without it pgloader creates a `zabbix` schema in PostgreSQL and loads
> everything there. The tables exist, the row counts are right, and Zabbix
> cannot see any of it because it looks in `public`.

Excluding the history tables here keeps this step short — configuration is
small. History is backfilled separately in step 7, after cutover, so downtime
is minutes rather than hours.

```bash
pgloader zabbix.load
```

Compare row counts between the two databases before continuing.

## 6. Hypertables, then cut over

Convert while the tables are still empty — it is instant. Converting a populated
table means rewriting every row.

```bash
cat /usr/share/zabbix/sql-scripts/postgresql/timescaledb/schema.sql | \
    sudo -u zabbix psql zabbix
```

Reset sequences. Most Zabbix ids come from its own `ids` table, but the
changelog sequence is a real PostgreSQL sequence and must be advanced:

```sql
SELECT setval('changelog_changelogid_seq',
              COALESCE((SELECT MAX(changelogid) FROM changelog), 1));
```

Point Zabbix at PostgreSQL:

```ini
# /etc/zabbix/zabbix_server.conf
DBHost=localhost
DBName=zabbix
DBUser=zabbix
DBPassword=...
```

```php
// /etc/zabbix/web/zabbix.conf.php
$DB['TYPE'] = 'POSTGRESQL';
```

```bash
systemctl restart zabbix-server
```

> **Install `php-pgsql` before you restart the frontend.** The server will come
> up happily on PostgreSQL while the web UI and API return
> *"DB type POSTGRESQL not supported"*, which reads like a Zabbix bug and is
> just a missing PHP extension.
>
> ```bash
> apt-get install -y php8.1-pgsql
> systemctl restart php8.1-fpm apache2
> ```

Also note: installing `zabbix-server-pgsql` **overwrites** the `-mysql` server
binary. After that package lands you are running the PostgreSQL binary whether
or not you have finished cutting over.

## 7. Backfill history

With the server already live on PostgreSQL and collecting new data, move the
history in the background:

```
LOAD DATABASE
    FROM mysql://zabbix:PASSWORD@localhost/zabbix
    INTO postgresql://zabbix:PASSWORD@localhost/zabbix
WITH data only, disable triggers
SET work_mem to '512MB', maintenance_work_mem to '2GB'
ALTER SCHEMA 'zabbix' RENAME TO 'public'
INCLUDING ONLY TABLE NAMES MATCHING 'history', 'history_uint', 'history_str',
                                    'trends', 'trends_uint', 'auditlog';
```

```bash
nohup pgloader history.load > /root/zbx_backfill.log 2>&1 &
```

Reference figures from this migration — 39.7M rows in 13m39s:

| Table | Rows |
|---|---:|
| `history` | 21.5M |
| `history_uint` | 15.8M |
| `history_str` | 2.77M |
| `trends` | 2.7M |
| `trends_uint` | 2.2M |
| `auditlog` | 1.9M |

## 8. Compression and retention

This is the payoff. Without it you have PostgreSQL with extra steps.

*Administration → Housekeeping → Override item history/trend period*, enable
compression and set "Compress records older than" to `7d`.

Verify:

```sql
SELECT * FROM timescaledb_information.compression_settings;
SELECT hypertable_name, count(*) FROM timescaledb_information.chunks
GROUP BY hypertable_name;
```

## 9. Only now, decommission

Leave MariaDB running until you have confirmed over several days that graphs
render correctly, historical data is present at the expected depth, and no
items are silently failing. Then:

```bash
systemctl stop mariadb && systemctl disable mariadb
# once you are certain:
# mysql -e "DROP DATABASE zabbix;"
```

---

## Traps, collected

| Symptom | Cause |
|---|---|
| Tables load, Zabbix sees nothing | pgloader created a `zabbix` schema — needs `ALTER SCHEMA ... RENAME TO 'public'` |
| Frontend: *DB type POSTGRESQL not supported* | `php-pgsql` not installed |
| Server refuses to start, unsupported TimescaleDB | 2.29+ against Zabbix 7.4 — pin 2.28 or set `AllowUnsupportedDBVersions=1` |
| Hypertable conversion takes hours | Ran against populated tables; convert while empty |
| Duplicate key on changelog | `changelog_changelogid_seq` not advanced after import |
| Server on PG before you cut over | `zabbix-server-pgsql` overwrote the `-mysql` binary |
