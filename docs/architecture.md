# Architecture

> Status: design for Phases 0–6 (AWS). Azure is described in [Multi-cloud](#multi-cloud).

## Goals

- **Secure from the first boot:** instances receive agents and a hardened baseline automatically.
- **Controlled change:** patches move through rings with explicit human approval before production.
- **Recoverable:** every change is preceded by a snapshot and followed by health checks; failures roll back.
- **Many accounts, one control point:** a central hub governs any number of workload accounts.
- **Declarative and auditable:** everything is defined in Git; every action leaves evidence.
- **Cloud-agnostic core:** adding a cloud means adding a provider, not rewriting the platform.

## Non-goals (for now)

- Managing containers or serverless workloads (Patchgate targets VMs).
- Replacing an EDR/antivirus — Patchgate installs and verifies the ones you choose.
- Multi-region active/active control plane.

## Building blocks

```mermaid
flowchart TB
    subgraph Core["core/ (cloud-agnostic)"]
        SCH[Schemas]
        CAT[Catalog<br/>agents · profiles · policies]
        SCR[In-VM scripts<br/>installers · Chocolatey · health checks]
        HRD[Hardening<br/>Ansible · DSC]
        LIB[Python lib<br/>rings · approvals · compliance · CloudEvents]
    end
    subgraph Prov["providers/aws/"]
        IAC[Terraform + StackSets]
        WF[Step Functions workflows]
        FN[Lambda adapters]
        SSMD[SSM documents]
    end
    CAT --> WF
    LIB --> FN
    SCR --> SSMD
    HRD --> SSMD
    SCH -. validates .-> CAT
```

## AWS deployment model: hub and spoke

```mermaid
flowchart LR
    subgraph Org["AWS Organizations"]
        MGMT["Management account<br/>billing · Organizations only"]
        subgraph SecOU["OU: Security"]
            HUB["Hub (tooling) account"]
        end
        subgraph WlOU["OU: Workloads"]
            DEV["Spoke: dev"]
            PROD["Spoke: prod"]
        end
    end
    MGMT -- "StackSets (service-managed,<br/>auto-deploy to OU)" --> DEV
    MGMT -- " " --> PROD
    DEV -- "EC2 events" --> HUB
    PROD -- "EC2 events" --> HUB
    HUB -- "AssumeRole Patchgate-Execution" --> DEV
    HUB -- " " --> PROD
```

| Account | Contains |
|---|---|
| **Management** | Organizations, StackSets administration, billing. No workloads, no Patchgate runtime. |
| **Hub** | Central EventBridge bus, Step Functions workflows, approval API, Lambdas, SNS, inventory/evidence bucket, Image Builder pipeline, delegated admin for Security Hub and Inspector. |
| **Spoke** (each workload account) | `Patchgate-Execution` role (trusts hub roles only), EventBridge rule forwarding EC2 events to the hub, SSM documents, instance profile, CloudWatch log groups. |

**Single-account mode:** hub and spoke resources are deployed into the same account. The workflows are identical; only the account list changes ([ADR 0002](adr/0002-hub-and-spoke-multi-account.md)).

**Onboarding:** spoke resources are a CloudFormation StackSet with automatic deployment to the Workloads OU. A new account created in that OU is managed without manual steps.

## Flows

### Bootstrap (Day 1)

```mermaid
sequenceDiagram
    participant EC2 as Instance (spoke)
    participant EB as EventBridge (spoke → hub)
    participant SF as Step Functions (hub)
    participant L as Lambda resolve (hub)
    participant SSM as SSM Run Command (spoke)
    EC2->>EB: state = running
    EB->>SF: start bootstrap execution
    SF->>L: describe instance (assume Patchgate-Execution)
    L-->>SF: managed? profile, OS, SSM ready
    alt not managed or already bootstrapped
        SF-->>SF: end
    else SSM agent not online yet
        SF->>SF: wait and retry (bounded)
    end
    SF->>SSM: run install-profile (agents, Chocolatey, hardening)
    SSM->>EC2: execute core scripts
    SF->>SSM: poll status
    SF->>EC2: tag Patchgate:BootstrapStatus
    SF->>SF: emit CloudEvent + notify
```

Triggered by events rather than `user_data` so every run is visible, retried and auditable ([ADR 0004](adr/0004-event-driven-bootstrap.md)).

### Patching with approval (Day 2)

```mermaid
flowchart TD
    S[Scheduled start] --> SC[Scan all rings]
    SC --> R0[Ring 0 — dev]
    R0 --> SN0[Snapshot] --> P0[Patch OS + Chocolatey] --> H0{Health check}
    H0 -- fail --> RB0[Rollback + alert]
    H0 -- ok --> SOAK[Soak time]
    SOAK --> AP{Email approval<br/>task token}
    AP -- rejected / timeout --> STOP[Stop + report]
    AP -- approved --> R1[Ring 1 — prod]
    R1 --> SN1[Snapshot] --> P1[Patch] --> H1{Health check}
    H1 -- fail --> RB1[Rollback + alert]
    H1 -- ok --> REP[Evidence report]
```

Approval: SNS sends an email with signed approve/reject links → API Gateway → Lambda calls `SendTaskSuccess`/`SendTaskFailure` with the Step Functions task token.

### Emergency patch (Phase 6)

An Inspector finding with CVSS ≥ 9 on a managed instance starts the patch workflow for the affected instances only, with a shortened soak time and an expedited approval.

## Identity and permissions

- Hub roles (`patchgate-hub-orchestrator`, `patchgate-hub-resolver`) may assume only `Patchgate-Execution`, and only in accounts of the organization (`aws:ResourceOrgID`), so onboarded accounts work without hub changes ([ADR 0006](adr/0006-org-wide-hub-access-and-stackset-spokes.md)).
- `Patchgate-Execution` trusts those exact hub role ARNs and allows:
  - `ssm:SendCommand` only with `Patchgate-*` documents and on instances tagged `Patchgate:Managed=true`;
  - `ec2:CreateTags` only for tag keys `Patchgate:*`;
  - read-only describe calls.
- CI/CD uses GitHub OIDC; no long-lived access keys.

See the [threat model](security/threat-model.md).

## Tags

| Tag | Set by | Meaning |
|---|---|---|
| `Patchgate:Managed` | User | `true` opts the instance in |
| `Patchgate:Profile` | User | Catalog profile to apply |
| `Patchgate:Env` | User | Environment label (`dev`, `prod`, …) |
| `Patchgate:Ring` | User or profile | Patch ring number |
| `Patchgate:BootstrapStatus` | Patchgate | `Success` / `Failed` |
| `Patchgate:LastPatch` | Patchgate | Timestamp and result of the last patch |

## Multi-cloud

The core (catalog, scripts, hardening, rules) is shared. Each cloud has its own provider implementing the [provider contract](../providers/README.md) with native services:

| Concern | AWS | Azure |
|---|---|---|
| Accounts | Organizations + OUs | Management groups + subscriptions |
| Cross-account access | AssumeRole | Lighthouse / managed identity |
| Run commands | SSM Run Command | VM Run Command / Arc |
| Patching | SSM Patch Manager | Azure Update Manager |
| Drift | State Manager | Azure Policy machine configuration |
| Orchestration | Step Functions | Durable Functions / Logic Apps |
| Events | EventBridge | Event Grid |
| Images | EC2 Image Builder | Azure Image Builder + Compute Gallery |
| Vulnerabilities | Inspector | Defender for Cloud |

Phase 7 manages Azure VMs as **SSM hybrid nodes** from the AWS hub to validate that the core is truly cloud-agnostic. Phase 8 adds a native Azure provider.

## Cost guardrails

- Workload region `us-east-2` (same pricing as `us-east-1`); the organization region SCP blocks most regional services in `us-east-1` for member accounts. Management-account resources (state bucket, CloudTrail, budgets) live in `us-east-1`.
- Serverless control plane (pay per use).
- No NAT Gateway: lab instances use a public subnet with no inbound rules and IMDSv2.
- AWS Budget with alerts and a Budget Action that blocks new instances above the limit.
- Ephemeral lab: created per test session, destroyed afterwards; a TTL reaper terminates instances older than 2 hours.
- `lab` is disposable and destroyed after each test session.
