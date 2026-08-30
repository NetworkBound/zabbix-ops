# Zero-touch onboarding

The goal: create a new VM or container and have it monitored, correctly
templated, and in the right host group without anyone opening the Zabbix UI.

There are two independent mechanisms, and it is worth running both because they
fail in different ways.

---

## 1. Agentless — Proxmox low-level discovery

**This is the one that gives you coverage for free.** Link the stock
`Proxmox VE by HTTP` template to a host representing each Proxmox node. Zabbix
then discovers every VM and container through the Proxmox API and creates items
for them automatically — no agent, no per-guest configuration, nothing to
install inside the guest.

A brand-new container appears in monitoring on the next discovery cycle purely
because it exists.

### Setup

On the Proxmox node, create a read-only API token:

```bash
pveum user add zabbix@pve
pveum aclmod / --users zabbix@pve --roles PVEAuditor
pveum user token add zabbix@pve monitoring --privsep 0
```

In Zabbix, create a host for the node, link `Proxmox VE by HTTP`, and set these
macros on the **host object** (not in a template, and not in this repo):

| Macro | Value |
|---|---|
| `{$PVE.URL.HOST}` | node address |
| `{$PVE.URL.PORT}` | `8006` |
| `{$PVE.TOKEN.ID}` | `zabbix@pve!monitoring` |
| `{$PVE.TOKEN.SECRET}` | the token secret — mark the macro **Secret text** |

> Expect roughly 395 discovered items per node on an estate of this size. Raise
> `StartHTTPPollers` on the server if the API items start queueing; the default
> of 1 saturates quickly once discovery is producing this many HTTP items.

### Two failure modes worth knowing

**`Cannot perform request: URL using bad/illegal format`** — `{$PVE.URL.PORT}`
is unset. Zabbix builds a URL without a port and libcurl rejects it.

**`Operation timed out after 3003 milliseconds` with 0 bytes received** — this
looks like a network problem and is not. The token secret is wrong. Proxmox
deliberately delays an unauthorised API response by about three seconds, which
lands just past Zabbix's 3-second HTTP timeout. You get a timeout instead of a
401, so every item fails as "unreachable" while the node is perfectly healthy.

If items time out at *almost exactly* 3 seconds, check the credential before
you check the network.

---

## 2. Agent-based — active auto-registration

Agentless discovery gives you the guest's *outside* view. For anything inside
the guest — load average, filesystem usage, process counts — you need the agent.

An agent configured with `ServerActive` and `HostMetadata` announces itself. An
auto-registration action matches on that metadata and creates the host with the
right templates and groups.

### Agent side

`scripts/install-agent.sh` writes the two lines that matter:

```ini
ServerActive=<zabbix-server-or-proxy>
HostMetadata=Linux
```

`HostMetadata` is what the action matches on. Anything more specific works too —
`Linux;lxc;prod` lets you route different guest classes to different templates.

### Server side

*Alerts → Actions → Autoregistration actions → Create action*

| | |
|---|---|
| **Condition** | Host metadata `contains` `Linux` |
| **Operation 1** | Add host |
| **Operation 2** | Add to host group `Homelab/Containers` |
| **Operation 3** | Link template `Homelab LXC Container` |

Enable the action. New agents self-register within about two minutes.

Add a second action with a narrower condition ahead of it to special-case a
class of guest — Zabbix evaluates actions in order, so put the specific ones
first.

### Making it truly zero-touch

Auto-registration only fires for guests that already have the agent. Bake it
into the template you clone from, or into the provisioning step:

```bash
# In your container template, once
ZBX_SERVER=10.0.0.20 ./scripts/install-agent.sh
```

If you are retrofitting an existing estate, `scripts/bulk-install-agents.sh`
covers every running LXC on a node in one pass.

---

## Verifying

```bash
# Does the agent answer?
zabbix_get -s <guest-ip> -k agent.ping

# Did the guest register?
python3 scripts/inventory.py | grep <name>

# Watch registration events
tail -f /var/log/zabbix/zabbix_server.log | grep -i autoreg
```

If a host registers but stays "unknown", the server can reach the agent's
active checks but not its passive interface — check that the interface address
Zabbix stored is actually reachable from the server, which is exactly what
`inventory.py --check-dns` is for.
