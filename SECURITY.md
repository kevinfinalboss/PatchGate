# Security Policy

Patchgate runs with privileged access to servers and cloud accounts, so we take security reports seriously.

## Supported versions

Patchgate has no stable release yet. Security fixes are applied to the `main` branch only.

| Version | Supported |
|---|---|
| `main` | ✅ |
| Pre-releases (`v0.x`) | Latest only |

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for security problems.**

Report privately through **[GitHub Private Vulnerability Reporting](https://github.com/kevinfinalboss/Patchgate/security/advisories/new)** (Security tab → *Report a vulnerability*).

Please include:

- A description of the issue and its impact.
- Affected component (`core`, `providers/aws`, catalog, CI) and commit/version.
- Steps to reproduce or a proof of concept.
- Any suggested fix or mitigation.

## What to expect

| Step | Target |
|---|---|
| Acknowledgement | Within 3 business days |
| Initial assessment and severity (CVSS) | Within 7 business days |
| Fix or mitigation for critical/high issues | Within 30 days, when feasible |
| Public advisory | After a fix is available, coordinated with the reporter |

We credit reporters in the advisory unless you prefer to stay anonymous.

## Scope

In scope:

- Privilege escalation through Patchgate roles, policies or trust relationships.
- Command injection through tags, catalog entries or approval links.
- Bypassing approval gates or forging approval tokens.
- Installing unverified artifacts (checksum bypass).
- Secrets exposed in logs, outputs, state or events.
- Supply-chain issues in CI/CD workflows of this repository.

Out of scope:

- Vulnerabilities in AWS, Azure or third-party agents themselves (report them to the vendor).
- Findings that require an already compromised hub account administrator.
- Missing hardening in the disposable `lab/` environment, unless it affects real deployments.

## Safe harbor

We will not pursue legal action for good-faith research that follows this policy, avoids privacy violations and service disruption, and only uses accounts you own.

## Security design

The security principles and threats Patchgate is designed against are documented in the [threat model](docs/security/threat-model.md).
