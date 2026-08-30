#!/usr/bin/env bash
#
# PostgreSQL streaming replication for a Zabbix HA cluster.
#
# Zabbix's own HA gives you automatic *server* failover, but every node shares
# one database. This replicates that database to a second site so the shared
# dependency is no longer a single point of failure.
#
#   On the PRIMARY (site A), first:
#       sudo ./setup-replication.sh primary --standby-ip 10.0.1.20 --dry-run
#       sudo ./setup-replication.sh primary --standby-ip 10.0.1.20
#
#   Then on the STANDBY (site B):
#       sudo ./setup-replication.sh standby --primary-ip 10.0.0.20
#
# The primary side is additive and safe: it creates a role, a slot, and one
# pg_hba line. It never touches your data and never restarts PostgreSQL unless a
# setting genuinely has to change.
#
# The standby side is DESTRUCTIVE — pg_basebackup wipes the target data
# directory. It refuses to run against a server holding a Zabbix database unless
# you pass --i-know-this-erases-the-target.
#
set -euo pipefail

MODE="${1:-}"; shift || true
STANDBY_IP=""; PRIMARY_IP=""; DRY_RUN=0; SLOT="zabbix_standby"; ERASE_OK=0
REPL_USER="replicator"
PGDATA_DEFAULT="/var/lib/postgresql/16/main"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --standby-ip) STANDBY_IP="$2"; shift 2 ;;
        --primary-ip) PRIMARY_IP="$2"; shift 2 ;;
        --slot)       SLOT="$2"; shift 2 ;;
        --pgdata)     PGDATA_DEFAULT="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --i-know-this-erases-the-target) ERASE_OK=1; shift ;;
        -h|--help)    sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "error: run as root." >&2; exit 1; }

# Commands are composed as strings (they contain nested quoting for su/psql),
# so eval is deliberate here rather than an oversight.
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  [dry-run] $*"
    else
        # shellcheck disable=SC2294  # intentional: argument is a command string
        eval "$*"
    fi
}

psql_q() { su - postgres -c "psql -tAX -c \"$1\""; }

# --------------------------------------------------------------------------
case "$MODE" in
primary)
    [[ -n "$STANDBY_IP" ]] || { echo "error: --standby-ip is required." >&2; exit 2; }
    echo "==> Preparing PRIMARY for a standby at ${STANDBY_IP}"

    wal_level=$(psql_q "show wal_level")
    senders=$(psql_q "show max_wal_senders")
    echo "    wal_level=${wal_level}  max_wal_senders=${senders}"

    NEEDS_RESTART=0
    if [[ "$wal_level" != "replica" && "$wal_level" != "logical" ]]; then
        echo "    wal_level must be 'replica' — will set it (requires a restart)"
        run "su - postgres -c \"psql -c \\\"alter system set wal_level = 'replica'\\\"\""
        NEEDS_RESTART=1
    fi
    if [[ "${senders:-0}" -lt 5 ]]; then
        echo "    raising max_wal_senders to 10 (requires a restart)"
        run "su - postgres -c \"psql -c 'alter system set max_wal_senders = 10'\""
        NEEDS_RESTART=1
    fi

    # Replication role. Password is generated here and never echoed; the standby
    # reads it from the file this writes.
    if [[ $(psql_q "select count(*) from pg_roles where rolname='${REPL_USER}'") == "0" ]]; then
        echo "    creating replication role '${REPL_USER}'"
        if [[ $DRY_RUN -eq 0 ]]; then
            PW=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
            su - postgres -c "psql -q -c \"create role ${REPL_USER} with replication login password '${PW}'\""
            umask 077
            printf 'host=%s user=%s password=%s\n' "$(hostname -I | awk '{print $1}')" "$REPL_USER" "$PW" \
                > /root/.pg_replication_credentials
            chmod 600 /root/.pg_replication_credentials
            echo "    credentials written to /root/.pg_replication_credentials (0600)"
            echo "    copy that file to the standby by hand — do not commit it"
        else
            echo "  [dry-run] create role ${REPL_USER}"
        fi
    else
        echo "    replication role '${REPL_USER}' already exists"
    fi

    # A slot guarantees the primary keeps WAL the standby has not consumed.
    # It is also how you fill a disk: an inactive slot retains WAL forever.
    # scripts/ha.py reports inactive slots for exactly that reason.
    if [[ $(psql_q "select count(*) from pg_replication_slots where slot_name='${SLOT}'") == "0" ]]; then
        echo "    creating replication slot '${SLOT}'"
        run "su - postgres -c \"psql -q -c \\\"select pg_create_physical_replication_slot('${SLOT}')\\\"\""
    else
        echo "    replication slot '${SLOT}' already exists"
    fi

    HBA=$(psql_q "show hba_file")
    if ! grep -qs "${STANDBY_IP}.*replication" "$HBA"; then
        echo "    allowing replication from ${STANDBY_IP} in $(basename "$HBA")"
        run "cp '${HBA}' '${HBA}.bak-\$(date +%Y%m%d-%H%M%S)'"
        run "echo 'host replication ${REPL_USER} ${STANDBY_IP}/32 scram-sha-256' >> '${HBA}'"
    else
        echo "    pg_hba already allows replication from ${STANDBY_IP}"
    fi

    # listen_addresses must include something the standby can reach.
    listen=$(psql_q "show listen_addresses")
    if [[ "$listen" == "localhost" ]]; then
        echo "    listen_addresses is 'localhost' — the standby cannot connect."
        echo "    set it to '*' (or the LAN address) and restart. Not done"
        echo "    automatically: it changes the server's exposure."
    fi

    if [[ $NEEDS_RESTART -eq 1 ]]; then
        echo
        echo "    A restart is required for the changed settings:"
        echo "        systemctl restart postgresql"
    else
        run "su - postgres -c 'psql -q -c \"select pg_reload_conf()\"'"
        echo "    configuration reloaded; no restart needed"
    fi

    echo
    echo "Primary ready. Next, on the standby:"
    echo "    scp /root/.pg_replication_credentials root@${STANDBY_IP}:/root/"
    echo "    sudo ./setup-replication.sh standby --primary-ip <this host>"
    ;;

# --------------------------------------------------------------------------
standby)
    [[ -n "$PRIMARY_IP" ]] || { echo "error: --primary-ip is required." >&2; exit 2; }
    PGDATA="$PGDATA_DEFAULT"
    echo "==> Building STANDBY from primary ${PRIMARY_IP} into ${PGDATA}"

    [[ -f /root/.pg_replication_credentials ]] || {
        echo "error: /root/.pg_replication_credentials not found." >&2
        echo "       Copy it from the primary first." >&2
        exit 2
    }

    # Refuse to silently destroy a populated database.
    if [[ -d "$PGDATA" ]] && [[ -n "$(ls -A "$PGDATA" 2>/dev/null)" ]]; then
        if su - postgres -c "psql -tAX -c \"select 1 from pg_database where datname='zabbix'\"" 2>/dev/null | grep -q 1; then
            if [[ $ERASE_OK -eq 0 ]]; then
                echo "error: this server already holds a 'zabbix' database." >&2
                echo "       pg_basebackup will ERASE ${PGDATA}." >&2
                echo "       Re-run with --i-know-this-erases-the-target if that is intended." >&2
                exit 2
            fi
            echo "    !! --i-know-this-erases-the-target given; existing data will be destroyed"
        fi
    fi

    # shellcheck disable=SC1091
    PRIMARY_CONN=$(sed "s/^host=[^ ]*/host=${PRIMARY_IP}/" /root/.pg_replication_credentials)

    echo "    stopping postgresql"
    run "systemctl stop postgresql"
    echo "    clearing ${PGDATA}"
    run "rm -rf '${PGDATA}'"
    run "install -d -o postgres -g postgres -m 0700 '${PGDATA}'"

    echo "    running pg_basebackup (this streams the whole database; 5GB takes a few minutes)"
    run "su - postgres -c \"PGPASSWORD=\\\$(echo '${PRIMARY_CONN}' | tr ' ' '\\n' | sed -n 's/^password=//p') \
        pg_basebackup -h ${PRIMARY_IP} -U ${REPL_USER} -D '${PGDATA}' \
        -Fp -Xs -P -R -S ${SLOT}\""

    # -R writes standby.signal + primary_conninfo, so the server comes up as a
    # standby with no further configuration.
    echo "    starting postgresql as a standby"
    run "systemctl start postgresql"
    if [[ $DRY_RUN -eq 0 ]]; then
        sleep 5
        echo -n "    in recovery: "
        su - postgres -c "psql -tAX -c 'select pg_is_in_recovery()'"
        echo -n "    receiving WAL: "
        su - postgres -c "psql -tAX -c 'select count(*) from pg_stat_wal_receiver'"
    fi

    echo
    echo "Standby built. Verify from either side:"
    echo "    python3 scripts/ha.py --require-replication"
    ;;

*)
    echo "usage: $0 {primary|standby} [options]" >&2
    sed -n '2,26p' "$0" >&2
    exit 2
    ;;
esac
