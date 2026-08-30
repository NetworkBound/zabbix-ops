# Roadmap

This document records what the Zabbix tooling ecosystem already does well, what
it does badly, and which of those gaps this project intends to fill. It exists
so that work here is deliberate rather than duplicative.

Survey conducted August 2026 against Zabbix 7.4.

## Principles

1. Do not rebuild something that has a healthy maintainer. Integrate instead.
2. Prefer the operational read side. Confirming that monitoring is correct is
   less well served than configuring it.
3. Anything that writes to production must be gated, reversible, and honest
   about what it is about to destroy.
4. Standard library only, so the tools keep working when nothing else does.

---

## Already solved. Do not rebuild.

| Concern | Use instead |
|---|---|
| API client library | [`zabbix_utils`](https://github.com/zabbix/python-zabbix-utils) — official, sync and async, tracked against each release |
| Host, user, macro administration | [`zabbix-cli`](https://github.com/unioslo/zabbix-cli) — actively maintained, strong bulk operations |
| Server and agent deployment, host provisioning | [`community.zabbix`](https://github.com/ansible-collections/community.zabbix) Ansible collection |
| Template synchronisation with git | [ZabbixCI](https://github.com/retigra/ZabbixCI) — the only live project doing bidirectional template sync |
| Dashboards, graphing, weathermaps | [grafana-zabbix](https://github.com/grafana/grafana-zabbix), [grafana-network-weathermap-ng](https://github.com/allamiro/grafana-network-weathermap-ng) |
| Inventory as source of truth | [NetBox](https://netbox.dev) with [netbox-zabbix-sync](https://github.com/TheNetworkGuy/netbox-zabbix-sync) or [nbxsync](https://github.com/OpensourceICTSolutions/nbxsync) |
| Whole-database backup | [zabbix-backup](https://github.com/npotorino/zabbix-backup) or ordinary `pg_dump` |

Two observations worth recording. First, `pyzabbix` still works but lags on 7.x
testing, and the similarly named `py-zabbix` is abandoned — new work should use
`zabbix_utils`. Second, Terraform coverage is fragmented across roughly six
partial forks with no critical mass; Ansible is the pragmatic choice today.

---

## Confirmed gaps

Each of these was checked against current repositories and issue trackers rather
than assumed.

### 1. Export canonicalisation

**Status: unsolved. Nothing exposes this as a tool.**

A Zabbix YAML export is not stable enough to diff directly:

- The export version header changes on server upgrade, so every file appears to
  change at once.
- Object and key ordering is not guaranteed consistent between servers, so two
  servers holding identical configuration can produce different files.
- Whether default-valued fields are emitted has shifted between versions.
- Webhook media types embed JavaScript as a single quoted string, which is
  unreviewable in a diff.

Normalisation logic exists, but buried inside individual exporters rather than
available as a primitive. Without it every downstream diff, review and drift
check is noise, which is the usual reason config-versioning efforts are
abandoned.

**Intent:** a canonicaliser that takes any export and emits a deterministic form
— semantic sort keys rather than stringified comparison, defaults stripped
against a per-version schema, version header normalised, webhook JavaScript
extracted to sidecar files and re-embedded on import.

### 2. Semantic diff and destructive-change gating

**Status: unsolved.**

`configuration.importcompare` is not a safe basis for a promotion gate:

- It compares host groups and templates only. Other objects are reported as new
  even when they exist.
- It previews structural change but validates nothing semantic — not macro
  resolution, not item key correctness against the target, not trigger
  references to other hosts.
- Its output depends on the calling user's permissions, so a narrowly scoped CI
  account receives a misleading preview.

This project has already observed the practical consequence: `importcompare`
reported no changes for a host import that then failed outright because the host
referenced a proxy absent from the target.

**Intent:** diff canonical form against canonical form, classify changes as
additive, mutating or destructive, and require explicit approval for destructive
ones. Deleting a templated item deletes its history; that deserves a prompt, not
a log line.

### 3. Template testing

**Status: unsolved. No framework exists.**

Nothing lets you assert that a template behaves correctly before it reaches
production. The available aids are the frontend's item and trigger test buttons,
calculated items mirroring trigger expressions against real history, and
`zabbix_sender` driving dummy triggers. All manual.

The nearest prior art is [zbx_snmpsim](https://github.com/v-zhuravlev/zbx_snmpsim),
which replays recorded `snmpwalk` output through an SNMP simulator against a
containerised Zabbix. It is a testbed, not a test framework: no assertions, no
CI harness.

**Intent:** a test gate that imports a template into a disposable instance,
attaches it to a simulated host, and asserts on the result — all items
supported, discovery produced the expected prototypes, an injected value moves a
specific trigger into a problem state.

### 4. Promotion pipeline

**Status: no published, reusable implementation.**

The recommended shape is well documented — export, lint, preview, stage,
promote — but everyone who has built it has built it privately. ZabbixCI models
promotion through separate push and pull branches, which is the closest existing
approach, and it covers templates rather than whole configuration.

**Intent:** a workflow that treats git as the source of truth for templates,
gates promotion on the diff above, and reconciles in both directions. Production
gets edited during incidents; a pipeline that only pushes will either overwrite
that or drift from it permanently.

### 5. Configuration outside the export format

**Status: poorly served.**

Actions, media types, maintenance windows, global macros, services and event
correlation are not covered by `configuration.export`. The only established
approach is dumping raw API JSON, which carries environment-specific numeric IDs
and therefore does not promote between instances.

**Intent:** identifier-free, name-referenced serialisation for these object
classes with an idempotent apply. This is the least glamorous gap and the reason
most template pipelines quietly stop at templates.

### 6. SNMP template generation

**Status: badly solved. The canonical tool targets a Zabbix version three major
releases out of date.**

`mib2zabbix` emits Zabbix 3.x XML. Modern Zabbix wants YAML built around a
`walk[]` master item with dependent items and preprocessing, which is
substantially more poller-efficient. `SNMPWALK2ZABBIX` generates from a live
walk — the right idea, since most of a vendor MIB is unimplemented on any given
device — but produces output requiring heavy manual editing.

Official vendor templates are genuinely maintained but stop at chassis and
interfaces. Service-layer monitoring — service state, per-queue and policer
counters, OAM session state, per-VRF routing — is absent across every vendor
examined, and is exactly what a service provider needs.

**Intent:** treated as a separate project rather than scope for this one. Noted
here because it is the largest gap found and worth stating plainly.

### 7. Trap semantics and alarm correlation

**Status: plumbing works, semantics do not.**

Zabbix has no native trap receiver. The `snmptrapd` to file to server path is
serviceable. What is missing sits above it: traps are matched to hosts by source
address only, mapping traps to items is hand-written regex, and there is no
concept of a raise and clear pair. Equipment that emits alarm-raised and
alarm-cleared notifications must have that correlation rebuilt by hand for every
alarm type. [trap2json](https://github.com/bangunindo/trap2json) demonstrates the
right shape but stops short of generating the Zabbix-side objects.

### 8. Topology-derived trigger dependencies

**Status: not built anywhere, and cheap to build.**

Inventory systems know the physical topology. Zabbix supports trigger
dependencies. Nobody connects the two, which is why a single upstream fault
still produces one alert per device behind it rather than one alert.

Both APIs are adequate for this. Of everything in this list it has the best
ratio of operational value to implementation effort.

---

## Priority

Ordered by impact, not by ease.

| | Item | Rationale |
|---|---|---|
| 1 | Canonicaliser | Everything else depends on diffs being trustworthy |
| 2 | Semantic diff with destructive-change gating | Makes a promotion approval reviewable in seconds |
| 3 | Promotion pipeline, both directions | Closes the loop between git and a production instance that gets edited under pressure |
| 4 | Out-of-format object serialisation | Removes the reason pipelines stop at templates |
| 5 | Template test gate | Highest absolute value, largest effort |
| 6 | Topology-derived trigger dependencies | Best value-to-effort ratio; suppresses alarm storms |

Already delivered: inventory reconciliation, bulk problem triage, DNS auditing,
safety-gated production to test cloning, two-node HA with a floating address,
and agent rollout.

## Explicitly out of scope

- A Terraform provider. Real gap, high effort, and Ansible covers the need.
- A general administration CLI. `zabbix-cli` is healthy.
- Another API client. `zabbix_utils` is official and maintained.
- Dashboards. Grafana's plugin is better than anything worth writing here.
- Vendor-controller integrations tied to a single manufacturer's northbound API.
  Bespoke by nature and not reusable.
