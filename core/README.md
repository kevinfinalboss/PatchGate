# core/

The cloud-agnostic heart of Patchgate: **what** should be installed, hardened and patched, and the rules that govern it. Nothing here knows which cloud it runs on.

| Folder | Purpose |
|---|---|
| [`schemas/`](schemas/) | JSON Schemas for `Agent`, `Profile` and `PatchPolicy` |
| [`catalog/agents/`](catalog/agents/) | Installable agents (templates included) |
| [`catalog/profiles/`](catalog/profiles/) | Profiles selected by the `Patchgate:Profile` tag |
| [`catalog/policies/`](catalog/policies/) | Patch policies: rings, windows, approvers, rollback |
| [`scripts/`](scripts/) | Scripts executed inside the VM (Linux and Windows) |
| [`hardening/`](hardening/) | CIS hardening content (Ansible and DSC) |
| [`lib/`](lib/) | Python library: rings, approvals, compliance, CloudEvents |
| [`tests/`](tests/) | Unit tests |

## Example (draft, `v1alpha1`)

```yaml
# catalog/agents/example-edr.yaml
apiVersion: patchgate.io/v1alpha1
kind: Agent
metadata:
  name: example-edr
spec:
  platforms: [windows, linux]
  install:
    windows:
      method: msi
      source: s3://<artifacts-bucket>/agents/example-edr-1.2.3.msi
      sha256: "<sha256>"
      args: "/quiet LICENSE={{secret:example-edr-license}}"
    linux:
      method: package
      source: s3://<artifacts-bucket>/agents/example-edr-1.2.3.rpm
      sha256: "<sha256>"
  validate:
    windows: "Get-Service ExampleEdr"
    linux: "systemctl is-active example-edr"
```

```yaml
# catalog/profiles/web-windows.yaml
apiVersion: patchgate.io/v1alpha1
kind: Profile
metadata:
  name: web-windows
spec:
  agents: [cloudwatch-agent, example-edr]
  chocolatey:
    - name: 7zip
    - name: dotnet-8.0-runtime
      version: "8.0.11"
  hardening: cis-level1
  ring: 1
```

The schemas in `schemas/` are the source of truth; these examples will be kept in sync with them.
