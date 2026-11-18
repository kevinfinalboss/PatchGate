# 0009. Drift modes per category and a maintenance window for drift

- **Status:** Accepted
- **Date:** 2026-11-18

## Context

Drift checks ([ADR 0007](0007-pluggable-hardening-engines.md)) run on a per-profile interval with
one mode for everything: `enforce` corrects agents, apps and hardening; `audit` only reports.
Two needs came up after Phase 4a:

- Teams want apps corrected automatically but hardening only reported (or the reverse), on the
  same profile.
- In production, reinstalling an app or re-applying a CIS baseline during business hours is a
  change in itself. Corrections should happen in a maintenance window.

Operators also want to trigger a check now instead of waiting up to the interval.

## Options considered

1. **A `MaintenanceWindow` catalog kind referenced by profiles, with tag changes and bootstrap
   also deferred to the window.** Reusable windows, but a new kind, a "pending plan" state on
   instances and more cases to test. Deferring bootstrap gains nothing: a new instance has nothing
   to break.
2. **Inline window in the profile's `drift` block, used only by drift checks.** One schema change,
   evaluated inside the VM with the clock it already has; bootstrap and tag changes stay immediate.
3. **SSM maintenance windows to schedule the drift association.** Provider-specific (the window
   would not live in the cloud-agnostic catalog) and adds one window resource per profile and
   account.

## Decision

Option 2.

- `drift.agents`, `drift.apps`, `drift.hardening` (`enforce | audit`) override `drift.mode`,
  which stays the default. Compiled plans always carry the three effective modes, and `mode` is
  kept so older executors still work.
- `drift.window`: `days`, `start` (HH:MM), `duration` (1h–24h, may cross midnight) and a fixed
  `utcOffset`. Fixed offsets avoid time-zone databases, which Windows PowerShell 5.1 and minimal
  Linux images do not handle consistently.
- Outside the window every category runs in `audit`. The first check inside each window
  occurrence runs even if the interval has not elapsed (state `lastWindowDriftAt`), so a window
  shorter than the interval is never skipped.
- On-demand reconcile: the drift SSM document takes `Force=true` (validated with
  `allowedValues`) and `patchgate-cli instance reconcile` sends it. Force ignores the interval
  and the window: it is an explicit operator action.
- Drift events gain `modes`, `trigger` (`schedule | reconcile`) and `window`
  (`inside | outside`); `mode` becomes a human-readable summary used by the email.

## Consequences

- Additive schema change; existing profiles compile to the same behaviour (their plan digests
  change once because the plan now lists every category).
- Windows are not shared between profiles; if many profiles need the same window, a catalog kind
  can be introduced later without changing the plan format.
- Deferring tag changes to the window, and approving changes before they apply, belong to the
  approval engine (Phase 4c), not to drift.
