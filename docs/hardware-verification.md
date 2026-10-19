# Hardware verification

No command is bench-verified yet. Every hardware command must reference a capability record
marked `bench_verified` with a bench log cited below (AGENTS.md rule 2). Roobotto has no
override for unverified commands. First-time verification is done by the owner outside
Roobotto, following `implementation-plan.md` Section 10.2 and the bench test's Tech Order in
[`tech-orders/`](tech-orders/README.md). Each row below should cite the TO that produced it.

## Verified commands

| Capability record | Command | Date | Bench log (`captures/`) | Other recipients checked | Side effects |
|---|---|---|---|---|---|
| _none yet_ | | | | | |

## Verified input reports

A device's `reports` (ADR-015) say what its input messages mean. A mapped snapshot counts as a
GO on real hardware only when its snapshot report is `bench_verified` with a bench log cited
here. H-1 verifies the Helix snapshot report (CC number, channel, values, authoritative path).
H-2 verifies the preset report and the load-time snapshot behavior.

| Capability record | Report | Date | Bench log (`captures/`) | Path checked | Notes |
|---|---|---|---|---|---|
| _none yet_ | | | | | |
