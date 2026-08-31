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

**Status: built — `scripts/canon.py`.** Nothing else exposes this as a tool.

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

**Built:** `canon.py` emits a deterministic form using semantic sort keys,
strips empties, and removes the version header. It works in JSON rather than
YAML, since Zabbix handles both and the standard library parses one of them.

Measured on a template exported from two servers on different patch versions:
92 changed lines by line diff, 46 real changes semantically, nothing spurious.

Still to do: extracting webhook JavaScript to sidecar files so it is reviewable
in a diff.

### 2. Semantic diff and destructive-change gating

**Status: built — `canon.py diff` and `scripts/promote.py`.**

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

**Built:** the diff classifies every change as additive, mutating or
destructive, and names which removals lose collected data. `promote.py apply`
refuses destructive changes without `--allow-destructive`, and re-plans after
applying to confirm the server converged rather than trusting the import.

### 3. Template testing

**Status: partly built — `scripts/tmpltest.py`.** No other framework exists.

Nothing lets you assert that a template behaves correctly before it reaches
production. The available aids are the frontend's item and trigger test buttons,
calculated items mirroring trigger expressions against real history, and
`zabbix_sender` driving dummy triggers. All manual.

The nearest prior art is [zbx_snmpsim](https://github.com/v-zhuravlev/zbx_snmpsim),
which replays recorded `snmpwalk` output through an SNMP simulator against a
containerised Zabbix. It is a testbed, not a test framework: no assertions, no
CI harness.

**Built:** `tmpltest.py` imports into a disposable instance, links to a
throwaway host, and asserts that the template imports, links, creates items,
resolves trigger expressions, and produces discovery prototypes. Pointed at a
real address it also enables the items and checks none go unsupported.

Still to do: injecting a value to assert a specific trigger fires, and using an
SNMP simulator so the whole thing runs without real hardware.

### 4. Promotion pipeline

**Status: built — `promote.py` and `.github/workflows/promote.yml`.**

The recommended shape is well documented — export, lint, preview, stage,
promote — but everyone who has built it has built it privately. ZabbixCI models
promotion through separate push and pull branches, which is the closest existing
approach, and it covers templates rather than whole configuration.

**Built:** the workflow plans on a pull request and posts the diff as the job
summary, blocks destructive changes there, applies on merge, and re-plans to
confirm convergence. `promote.py drift` runs the comparison in reverse to catch
a server edited outside git.

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

**Status: built for the walk-based case — `scripts/walk2tmpl.py`.** Generating
from a MIB remains unsolved elsewhere.

`mib2zabbix` emits Zabbix 3.x XML. Modern Zabbix wants YAML built around a
`walk[]` master item with dependent items and preprocessing, which is
substantially more poller-efficient. `SNMPWALK2ZABBIX` generates from a live
walk — the right idea, since most of a vendor MIB is unimplemented on any given
device — but produces output requiring heavy manual editing.

Official vendor templates are genuinely maintained but stop at chassis and
interfaces. Service-layer monitoring — service state, per-queue and policer
counters, OAM session state, per-VRF routing — is absent across every vendor
examined, and is exactly what a service provider needs.

**Built:** `walk2tmpl.py` generates a template from an `snmpwalk` capture,
producing walk master items with dependent items and a discovery rule per table
rather than one item per OID. Verified end to end against a 24-port switch: 579
walked OIDs became 7 scalar items, 3 masters and one rule with 22 prototypes,
and discovery produced items named after the real ports.

Still to do: MIB parsing for readable names beyond the small built-in table, and
service-layer coverage for carrier equipment.

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

## What is left

Ordered by impact, not by ease.

| | Item | Why it matters |
|---|---|---|
| 1 | Serialisation for objects outside the export format | Actions, media types, maintenance windows and macros are not in `configuration.export`. This is the reason most template pipelines quietly stop at templates. |
| 2 | Trigger dependencies from inventory topology | The best ratio of value to effort on this list. It is the difference between one alert and forty when an upstream link fails. |
| 3 | Webhook JavaScript as sidecar files | Embedded as one quoted string, webhook code is unreviewable in a diff and untestable outside a live server. |
| 4 | Value injection in the test harness | Asserting that a specific trigger fires on a specific value, and an SNMP simulator so tests run without real hardware. |
| 5 | MIB-based name resolution | `walk2tmpl.py` names OIDs from a small built-in table; anything else keeps its numeric OID. Readable names need MIB parsing. |
| 6 | Service-layer templates for carrier equipment | Vendor templates stop at chassis and interfaces. Service state, queue and policer counters, and OAM sessions are absent everywhere. |

Delivered so far: inventory reconciliation, production-readiness auditing and
remediation, notification delivery verification, unsupported-item triage,
trigger expression linting, agent fleet auditing, bulk problem triage, DNS
auditing, safety-gated production to test cloning, export canonicalisation,
semantic diff with destructive-change gating, a promotion pipeline that plans on
review and confirms convergence after applying, template testing, SNMP template
generation from a device walk, two-node HA with a floating address that follows
the active node, and agent rollout.

## Explicitly out of scope

- A Terraform provider. Real gap, high effort, and Ansible covers the need.
- A general administration CLI. `zabbix-cli` is healthy.
- Another API client. `zabbix_utils` is official and maintained.
- Dashboards. Grafana's plugin is better than anything worth writing here.
- Vendor-controller integrations tied to a single manufacturer's northbound API.
  Bespoke by nature and not reusable.
