# core/

The cloud-agnostic heart of Patchgate: **what** should be installed, hardened and kept in place, and the rules that govern it. Nothing here knows which cloud it runs on.

| Folder | Purpose |
|---|---|
| [`schemas/`](schemas/) | JSON Schemas for `Agent`, `Profile`, `AppSet`, `Control`, `Baseline`, `RolloutPolicy` and `ApproverGroup` (`v1alpha1`) |
| [`catalog/agents/`](catalog/agents/) | Installable agents (templates included) |
| [`catalog/profiles/`](catalog/profiles/) | Profiles selected by the `Patchgate:Profile` tag |
| [`catalog/appsets/`](catalog/appsets/) | App bundles requested with the `Patchgate:AppSets` tag |
| [`catalog/controls/`](catalog/controls/) | Hardening controls for the native `controls` engine |
| [`catalog/baselines/`](catalog/baselines/) | Pinned third-party hardening content (`ansible`, `dsc` engines) |
| [`catalog/policies/`](catalog/policies/) | Rollout policies: rings, approval gates, soak, failure handling |
| [`scripts/`](scripts/) | Executors that run inside the VM (Linux and Windows) |
| [`lib/`](lib/) | Python library: plan composition, schema validation, rollout and approval rules, catalog CLI |
| [`tests/`](tests/) | Unit tests |

## Example profile

```yaml
# catalog/profiles/web-windows.yaml
apiVersion: patchgate.io/v1alpha1
kind: Profile
metadata:
  name: web-windows
spec:
  platform: windows
  agents: [cloudwatch-agent, microsoft-defender]
  apps:
    - name: 7zip
    - name: notepadplusplus
  allowedAppSets: [dotnet8, nodejs-lts]
  hardening:
    engine: controls
    level: baseline
  drift:
    interval: 30m        # off | 30m | 1h | 2h | 6h | 12h | 24h
    mode: enforce        # default for every category: enforce corrects, audit only reports
    hardening: audit     # optional override per category: agents, apps, hardening
    window:              # optional: outside the window every category only reports
      days: [mon, tue, wed, thu, fri]
      start: "22:00"
      duration: 4h       # 1h-24h, may cross midnight
      utcOffset: "-03:00"
```

### Drift

Drift checks re-validate agents, apps and hardening on the profile's `interval`.

- **Per category:** `mode` is the default; `agents`, `apps` and `hardening` override it.
- **Maintenance window:** outside it, every category only reports (`audit`). The first check
  inside each window occurrence runs even if the interval has not elapsed, so what was reported
  outside the window gets corrected. Bootstrap and tag changes are not affected by the window.
- **On demand:** `patchgate-cli instance reconcile <id>` runs a check now, ignoring the interval
  and the window.

See [ADR 0007](../docs/adr/0007-pluggable-hardening-engines.md) and
[ADR 0009](../docs/adr/0009-drift-modes-per-category-and-maintenance-window.md).

The schemas in `schemas/` are the source of truth; this example is kept in sync with them.
