# Threat model

Patchgate can run commands as root/SYSTEM on every managed server in every connected account. That makes it a high-value target. This document lists the main threats and the controls designed against them. Method: STRIDE over the main assets and trust boundaries.

## Assets

- Hub account and its roles (control over the entire fleet).
- `Patchgate-Execution` roles in spoke accounts.
- The catalog (defines what code runs on servers).
- Approval tokens and links.
- Secrets referenced by the catalog (licenses, agent CIDs).
- Inventory, logs and compliance evidence.

## Trust boundaries

1. GitHub repository and CI/CD → hub account.
2. Hub account → spoke accounts (AssumeRole).
3. Spoke account → instance (SSM agent).
4. Email recipient → approval API (internet-facing).
5. Instance tags (writable by spoke users) → hub workflows.

## Threats and controls

| # | Threat (STRIDE) | Scenario | Controls |
|---|---|---|---|
| T1 | Elevation of privilege | Spoke user tags an instance to make Patchgate run attacker-chosen commands | Tags only select **catalog** profiles; never interpolated into scripts without allow-list validation; SSM documents use `allowedPattern` |
| T2 | Tampering | Malicious change to the catalog installs a backdoor on every server | Branch protection, required reviews, CODEOWNERS on `core/catalog/`, `sha256` required, signed commits recommended |
| T3 | Tampering | Agent installer replaced at the download source | Checksum verified before install, fail closed; artifacts hosted in a versioned, private S3 bucket |
| T4 | Spoofing | Forged or replayed approval link approves a production rollout | Links carry an HMAC-SHA256 token naming the request and the approver, checked before any lookup; tokens expire with the approval timeout; each member decides once (conditional write); only members of the group count; GET only shows a confirmation page, the POST decides (mail scanners decide nothing); the Step Functions task token never leaves the hub; signing key created by the function, kept as a SecureString, never in Terraform state |
| T5 | Elevation of privilege | Compromised spoke uses its access to reach the hub | Spokes have no permissions in the hub except `PutEvents` on one bus; hub validates and normalizes every event |
| T6 | Elevation of privilege | Execution role abused for actions beyond Patchgate, or hub reaching accounts outside the organization | Least-privilege policy: only `Patchgate-*` documents, only `Patchgate:Managed=true` instances, only `Patchgate:*` tag keys; hub may assume the role only in org accounts (`aws:ResourceOrgID`) and each spoke trusts only the named hub roles |
| T7 | Spoofing | Long-lived CI credentials stolen | GitHub OIDC with repository/branch conditions; no access keys |
| T8 | Information disclosure | Secrets leaked in command output, logs or Step Functions history | Secrets resolved on the instance at runtime; scripts never echo secrets; execution data logging reviewed per state |
| T9 | Repudiation | No proof of who approved or what changed | Every request and decision (member, approve/reject, time) kept in `patchgate-approvals` with point-in-time recovery; configuration history; Step Functions history; CloudTrail in all accounts; evidence in a versioned bucket with Object Lock (Phase 6) |
| T10 | Denial of service | Bad patch breaks production fleet | Rings, soak time, health checks, snapshots and automatic rollback; production gated by approval |
| T11 | Denial of service | Event flood (mass launches) exhausts quotas | Bounded retries, concurrency limits and SQS buffering where needed |
| T12 | Denial of service / information disclosure | The approval API is probed, flooded or used to read requests | Only `GET`/`POST /approve` exist; stage throttling (2 req/s, burst 10); invalid signatures are refused before any table read; pages are `no-store`, `no-referrer`, strict CSP, not framable; the token is not written to access logs |
| T13 | Elevation of privilege | A spoke user picks a lenient approver group (or ring) with a tag to get a change approved | `Patchgate:Approvers` only accepts groups the account allows and `Patchgate:Ring` only where the account allows it; refused tags are reported; unregistered accounts are treated as the most protected ring |
| T14 | Tampering | A gated change reaches instances before approval (console edit, re-seeded built-in) | The CLI pins the current revision before writing; the audit function pins it for changes made elsewhere and starts the rollout; the resolver serves each ring and group only the revision released to it |

## Residual risks

- A hub administrator is effectively an administrator of the fleet. Protect the hub with SCPs, MFA, break-glass procedures and minimal human access.
- Third-party agents run with high privileges by design; Patchgate verifies integrity, not vendor trustworthiness.
- Approval is possession of the email: whoever holds an approver's link before it expires can decide for that approver. Strong authentication (SSO) on the approval page is out of scope for now.
- Hub administrators can read the signing key parameter and the approvals table, and could forge approvals. They can also change the configuration directly; this is the same trust as above.
- Between a change made outside `patchgate-cli` and the audit function pinning the previous revision (seconds), a bootstrap could still receive the new revision.

## Review

Revisit this document whenever a phase adds a new trust boundary. Last review: Phase 4c-1 (approval API, ADR 0010).

## Accepted risks (lab scale)

These controls are intentionally not implemented to keep the lab near zero cost. They are skipped in `.checkov.yaml` and should be enabled for production deployments.

| Control | Reason |
|---|---|
| KMS customer-managed keys for S3 and CloudTrail | US$1/month per key; SSE-S3 used instead |
| S3 server access logging | CloudTrail already records API access |
| S3 cross-region replication | Doubles storage; lab data is reproducible |
| CloudTrail → CloudWatch Logs, SNS notifications | Ingestion cost; logs analysed from S3 |
