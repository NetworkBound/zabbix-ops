# Running these tools on a schedule

The tools are useful run by hand. They are far more useful run every morning
without anyone remembering to, because the drift they catch is invisible until
something is already broken.

There is one constraint that shapes everything below: **the Zabbix and Proxmox
APIs live on a private network.** A hosted GitHub runner has no route to them.
So the work splits in two.

| Job | Where | Why |
|---|---|---|
| `ci.yml` — lint, unit tests, secret scan | hosted, or anywhere | Touches nothing but the repo. The tools keep their logic separate from their I/O precisely so this is possible. |
| `monitoring-drift.yml` — live reconcile | **self-hosted, inside the network** | Needs to reach the APIs. |

Both files live in `.github/workflows/`. Gitea Actions reads that directory when
`.gitea/workflows/` is absent and supports the same syntax, so one set of
workflows serves GitHub and a self-hosted Gitea equally.

---

## Option A — self-hosted GitHub Actions runner

Register a runner on any host inside the network that can reach both APIs.

```bash
# Repo → Settings → Actions → Runners → New self-hosted runner
mkdir actions-runner && cd actions-runner
curl -o actions-runner.tar.gz -L <url-from-that-page>
tar xzf actions-runner.tar.gz
./config.sh --url https://github.com/<owner>/homelab-zabbix --token <token>
sudo ./svc.sh install && sudo ./svc.sh start
```

The workflow targets `${{ vars.SELF_HOSTED_LABEL || 'self-hosted' }}`. If your
runner carries a different label, set the `SELF_HOSTED_LABEL` repository
variable rather than editing the workflow.

> A self-hosted runner executes whatever a workflow tells it to, on a machine
> inside your network. **Do not enable it for pull requests from forks** —
> anyone could open a PR that runs code on it. Repo → Settings → Actions →
> "Require approval for all outside collaborators".

## Option B — Gitea Actions runner

If you already run Gitea, this keeps everything inside the house — including
the secrets, which never leave your network.

```bash
# 1. Enable Actions in app.ini
[actions]
ENABLED = true

# 2. Get a registration token
#    Site Admin → Actions → Runners → Create new runner

# 3. On the runner host (needs Docker)
act_runner register --no-interactive \
    --instance http://gitea.internal:3000 \
    --token <REG_TOKEN> \
    --name homelab-runner \
    --labels ubuntu-latest:docker://node:20-bookworm,host:host
```

Give the runner **2 vCPU / 4 GB / 20 GB**. Do not host it on the Gitea container
itself if that container is small — the runner needs meaningfully more room than
Gitea does.

Then set `SELF_HOSTED_LABEL` to a label your runner actually advertises
(`host`, or `ubuntu-latest`).

Two differences worth knowing before you debug something that is not broken:

- `actions/checkout@v4` and friends resolve through Gitea's configured action
  proxy. If your runner has no outbound internet, mirror the actions locally or
  replace the checkout step with a plain `git clone`.
- Job summaries (`$GITHUB_STEP_SUMMARY`) have partial support depending on
  version. The workflow falls back to stdout, so the output is never lost.

---

## Configuration

Set these on the repository. Anything that is a credential is a **secret**;
everything else is a **variable**, so it stays readable in logs where that helps.

| Name | Kind | Example |
|---|---|---|
| `ZBX_URL` | secret | `http://10.0.0.20/zabbix/api_jsonrpc.php` |
| `ZBX_TOKEN` | secret | a scoped API token, not a password |
| `PVE_0_URL` | secret | `https://10.0.0.10:8006` |
| `PVE_0_TOKEN` | secret | `zabbix@pve!monitoring=…` |
| `PVE_0_NAME` | variable | `pve1` |
| `PVE_1_*` | as above | second node, optional |
| `EXCLUDE_GROUPS` | variable | `Homelab/Network Homelab/Infrastructure` |
| `SELF_HOSTED_LABEL` | variable | `self-hosted`, `host`, … |

Use a **read-only** Zabbix API token (Users → API tokens) and a **PVEAuditor**
Proxmox token. Nothing in the scheduled job writes to either system, so nothing
in the scheduled job needs permission to.

The URLs are marked secret rather than variable on purpose: they name internal
hosts, and workflow logs are the easiest place for that to escape.

## What the scheduled job does

1. Verifies every required secret is present, and **fails loudly if not** —
   otherwise a missing secret produces an empty report that looks clean.
2. `zbx.py` — connectivity and API version.
3. `reconcile.py` — the real work. Proxmox inventory versus Zabbix inventory.
4. `inventory.py --check-dns` — forward/reverse DNS drift (non-blocking).
5. `problems.py list` — current high-severity problems (non-blocking).
6. Writes a job summary, uploads the reports as an artifact for 30 days.
7. Fails the run if `no_address` or `drift` findings exist.

Steps 4 and 5 are `continue-on-error` deliberately: a resolver hiccup should not
mask the reconcile result, which is the finding that matters.

### Tuning what fails the build

Start permissive and tighten. On a lived-in estate the first run will find
things, and a job that is red on day one gets ignored by day three.

```yaml
--fail-on no_address drift          # default: only what is unambiguously wrong
--fail-on no_address                # start here if drift is noisy
--fail-on no_address drift unmonitored orphaned   # once you are clean
```

`unmonitored` and `orphaned` are the noisy ones — the first flags every guest you
deliberately do not monitor, the second every device that is not a Proxmox guest.
Curate `EXCLUDE_GROUPS` before you gate on either.

## Notifications

The workflow deliberately does not ship a notifier — everyone has a different
one. Add a step; the reconcile output is already on disk as JSON:

```yaml
- name: Notify
  if: failure()
  run: |
    curl -s -H "Title: Zabbix drift detected" \
         -d "$(python3 -c '
    import json
    r = json.load(open("reconcile.json"))["totals"]
    print(f"{r[\"no_address\"]} unreachable-by-config, {r[\"drift\"]} drifted")')" \
         "https://ntfy.example/homelab"
```

## Running it by hand first

Do this before scheduling anything. It tells you what your estate actually looks
like, and lets you curate `EXCLUDE_GROUPS` so the scheduled version is signal
rather than a wall of expected findings.

```bash
set -a; . ./.env; set +a
python3 scripts/reconcile.py
python3 scripts/reconcile.py --exclude-group Homelab/Network Homelab/Infrastructure
```
