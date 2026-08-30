# Troubleshooting

Failures that cost real time, and what they actually were.

---

## API

### Every call fails with a permission error after upgrading to 7.x

The token from `user.login` must be sent as a header, not in the request body:

```python
# Zabbix <= 6.4 — no longer works
{"jsonrpc": "2.0", "method": "host.get", "auth": token, ...}

# Zabbix >= 7.0
headers = {"Authorization": f"Bearer {token}"}
```

The `auth` field is ignored rather than rejected, so the error you get talks
about permissions and never mentions authentication. `scripts/zbx.py` uses the
header form.

### `apiinfo.version` fails while everything else works

`apiinfo.version` is the one method that must **not** carry an auth header.
Sending one makes it fail.

---

## Proxmox integration

### `Cannot perform request: URL using bad/illegal format`

`{$PVE.URL.PORT}` is not set on the host object. Zabbix builds a URL with no
port and libcurl rejects it before any request is made.

### `Operation timed out after 3003 milliseconds`, 0 bytes received

**The token secret is wrong.** This is not a network problem.

Proxmox delays an unauthorised API response by about three seconds. That lands
just past Zabbix's 3-second HTTP timeout, so an authentication failure surfaces
as a timeout on every item at once while the node is completely healthy.

The tell is the timing: *almost exactly* 3000 ms, on every item, starting at the
same moment. Re-issue the token and update the macro.

### All Proxmox items go unsupported after a template update

Zabbix does not automatically retry unsupported items on a schedule you would
notice. After fixing the underlying cause, clear them:

*Data collection → Hosts → Items → filter State: Not supported → Execute now*

---

## Agents

### Host shows "unknown" availability forever

Availability reflects the **passive** interface. An agent doing only active
checks reports data while its interface stays grey.

Either add a passive check, or accept grey and alert on `nodata()` against a
real item instead. Note that a permanently-grey interface is often the
`0.0.0.0` case below rather than an active-checks-only design.

### HIGH "unreachable" on a host that is demonstrably up

Check the host's interface address before anything else:

```bash
./scripts/reconcile.py --only no_address
./scripts/reconcile.py --only drift
```

Two causes, both of which look exactly like an outage:

* **The interface is `0.0.0.0`.** The host was registered without a real
  address — see [auto-registration.md](auto-registration.md). Nothing you do to
  the guest will clear the alert.
* **The address is stale.** The guest was renumbered and Zabbix still has the
  old one. `reconcile.py` compares against what Proxmox actually has.

The tell for both is that the service itself answers fine on its own port while
Zabbix insists the host is down.

### Agent runs, but the server gets nothing

Work outward:

```bash
systemctl status zabbix-agent2                 # is it up?
grep -E '^(Server|ServerActive|Hostname)=' /etc/zabbix/zabbix_agent2.conf
zabbix_get -s <guest-ip> -k agent.ping         # from the server
tail -50 /var/log/zabbix/zabbix_agent2.log
```

The usual cause is `Hostname` in the agent config not matching the host name in
Zabbix. Active checks are matched by that string, and a mismatch fails silently
— the agent is healthy, the server just has nowhere to file the data.

### Docker items fail on a host where Docker works

The `zabbix` user needs access to the Docker socket:

```bash
usermod -aG docker zabbix
systemctl restart zabbix-agent2
```

---

## False positives

### A CPU trigger fires while the host is idle

Check whether the item measures **idle** or **utilisation**. Wrapping an
idle-percentage item in a `>80` trigger produces an alert whenever the machine
is doing nothing — backwards, and it looks plausible enough to survive review.

### Dozens of "service not running" alerts on one host

A service-discovery rule matched things that are not meant to be running, or a
template intended for a different class of host got linked. Disable the triggers
rather than the items — you keep the data and lose the noise.

### Alerts for guests you intentionally stopped

Either disable the host in Zabbix, or move it to a group with different
alerting. `Homelab/Lab` exists for exactly this.

---

## Housekeeping and storage

### Database growing without bound

Check retention (*Administration → Housekeeping*) and confirm housekeeping is
actually completing — on a large non-partitioned `history` table it can take
longer than its own interval and effectively never finish.

The structural fix is TimescaleDB; see
[postgresql-timescaledb.md](postgresql-timescaledb.md).

### Poller queue backing up

*Administration → Queue*. If HTTP agent items dominate, raise
`StartHTTPPollers` — the default of 1 does not survive Proxmox LLD.

---

## Frontend

### *DB type POSTGRESQL not supported* after migrating

`php-pgsql` is missing. The server runs fine on PostgreSQL while the frontend
and API fail, which reads like a Zabbix bug and is not.

```bash
apt-get install -y php8.1-pgsql
systemctl restart php8.1-fpm apache2
```

### Alert links point at `http://localhost` or nowhere

The `{$ZABBIX.URL}` macro is unset, so webhook notifications build malformed
links. Set it at *Administration → Macros* to the URL your users actually reach
the frontend on.

### Admin account locked out

After too many failed attempts Zabbix blocks the account. Reset the hash
directly and restart the frontend:

```sql
UPDATE users SET passwd = '<bcrypt-hash>', attempt_failed = 0
WHERE username = 'Admin';
```

---

## Diagnostics

```bash
# Server health
systemctl status zabbix-server
tail -f /var/log/zabbix/zabbix_server.log

# Is the API alive, and what does it think it is?
python3 scripts/zbx.py

# What is actually broken right now?
python3 scripts/problems.py --min-severity 4 list

# Do monitored names still match reality?
python3 scripts/inventory.py --check-dns --only-problems
```
