#!/usr/bin/env bash
#
# Promote the standby database at site B to primary. THIS IS THE FAILOVER.
#
#   ./promote-standby.sh --check          # is promotion safe right now?
#   ./promote-standby.sh --promote        # do it
#
# Read this before you run it at 3am.
#
# Why this is not automatic
# -------------------------
# Zabbix fails its *servers* over automatically, in about a minute, and that is
# fine — the standby server does nothing until it takes the role.
#
# Database promotion is different. If site B promotes because it cannot reach
# site A, but site A is actually alive and merely unreachable, you now have two
# primaries accepting writes. That is a split brain, and the recovery is
# rebuilding one side from scratch and losing whatever it collected. A partition
# is indistinguishable from an outage from one side, so no automatic rule can
# get this right. A human confirming "site A really is down" can.
#
# So: automatic server failover, deliberate database promotion. That asymmetry
# is the design, not an omission.
#
# Promotion is ONE WAY. The old primary cannot simply be restarted afterwards —
# it must be rebuilt as a standby of the new primary (pg_rewind, or a fresh
# pg_basebackup).
#
set -euo pipefail

ACTION=""; PRIMARY_IP="${PRIMARY_IP:-}"; ASSUME_YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)      ACTION="check"; shift ;;
        --promote)    ACTION="promote"; shift ;;
        --primary-ip) PRIMARY_IP="$2"; shift 2 ;;
        --yes)        ASSUME_YES=1; shift ;;
        -h|--help)    sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ -n "$ACTION" ]] || { echo "usage: $0 --check | --promote [--primary-ip IP]" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "error: run as root." >&2; exit 1; }

psql_q() { su - postgres -c "psql -tAX -c \"$1\"" 2>/dev/null; }

echo "==> Standby state"
IN_RECOVERY=$(psql_q "select pg_is_in_recovery()" || echo "?")
if [[ "$IN_RECOVERY" != "t" ]]; then
    echo "error: this server is NOT a standby (pg_is_in_recovery = ${IN_RECOVERY})." >&2
    echo "       Nothing to promote. Are you on the right host?" >&2
    exit 2
fi

RECEIVING=$(psql_q "select count(*) from pg_stat_wal_receiver" || echo "0")
LAG=$(psql_q "select coalesce(extract(epoch from now() - pg_last_xact_replay_timestamp())::int, -1)" || echo "-1")
LAST_REPLAY=$(psql_q "select coalesce(pg_last_xact_replay_timestamp()::text,'never')" || echo "?")

echo "    in recovery    : yes"
echo "    receiving WAL  : ${RECEIVING} sender(s)"
echo "    last replayed  : ${LAST_REPLAY}"
echo "    replay lag     : ${LAG}s"

echo
echo "==> Is the old primary really gone?"
if [[ -n "$PRIMARY_IP" ]]; then
    if timeout 5 bash -c "echo > /dev/tcp/${PRIMARY_IP}/5432" 2>/dev/null; then
        echo "    !! ${PRIMARY_IP}:5432 is ACCEPTING CONNECTIONS."
        echo "       The old primary appears to be alive. Promoting now would give"
        echo "       you two primaries and a split brain."
        REACHABLE=1
    else
        echo "    ${PRIMARY_IP}:5432 is unreachable — consistent with a real outage."
        REACHABLE=0
    fi
else
    echo "    (no --primary-ip given; cannot check. Pass it — this is the check"
    echo "     that prevents a split brain.)"
    REACHABLE=-1
fi

if [[ "$ACTION" == "check" ]]; then
    echo
    if [[ "${REACHABLE}" == "1" ]]; then
        echo "VERDICT: do NOT promote. The old primary is reachable."
        exit 1
    fi
    if [[ "${LAG}" -gt 300 ]] 2>/dev/null; then
        echo "VERDICT: promotion possible, but this standby is ${LAG}s behind —"
        echo "         you will lose roughly that much monitoring data."
        exit 1
    fi
    echo "VERDICT: promotion looks safe. Re-run with --promote."
    exit 0
fi

# ---- promote ----
if [[ "${REACHABLE}" == "1" && $ASSUME_YES -eq 0 ]]; then
    echo
    echo "REFUSING: the old primary is reachable. Stop PostgreSQL there first," >&2
    echo "          or pass --yes if you are certain it is fenced." >&2
    exit 2
fi

if [[ $ASSUME_YES -eq 0 ]]; then
    echo
    echo "About to promote this standby to PRIMARY. This is one way."
    read -r -p "Type PROMOTE to continue: " answer
    [[ "$answer" == "PROMOTE" ]] || { echo "aborted."; exit 1; }
fi

echo
echo "==> Promoting"
su - postgres -c "pg_ctl promote -D \$(psql -tAX -c 'show data_directory')" 2>/dev/null \
    || su - postgres -c "psql -c 'select pg_promote(true, 60)'"

sleep 5
NOW=$(psql_q "select pg_is_in_recovery()")
if [[ "$NOW" == "f" ]]; then
    echo "    promoted — this server is now the primary"
else
    echo "error: still in recovery after promotion. Check the PostgreSQL log." >&2
    exit 1
fi

cat <<'EOF'

Promoted. Remaining steps, in order:

  1. Point the Zabbix servers at this database.
     Every HA node must use the SAME DBHost, so update zabbix_server.conf on
     both and restart. Until you do, the cluster is still trying to reach a
     database that is gone.

  2. Confirm the cluster came back:
         python3 scripts/ha.py

  3. Rebuild the old site as a standby of THIS server once it returns.
     It cannot simply be restarted — it believes it is a primary, and starting
     it while this one is live is exactly the split brain you just avoided.
         pg_rewind, or a fresh: ./setup-replication.sh standby --primary-ip <this host>

  4. Recreate the replication slot here for the new standby.
EOF
