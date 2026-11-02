# Hardware verification

No command is bench-verified yet. Every hardware command must reference a capability record
marked `bench_verified` with a bench log cited below (AGENTS.md rule 2). Roobotto has no
override for unverified commands. First-time verification is done by the owner outside
Roobotto, following `implementation-plan.md` Section 10.2.

The bench-worksheet screen of `roobotto ui`, and its CLI twin `roobotto bench record`, append the
rows below and change the matching capability record's `status` and `evidence` in the rig file.
Neither sends MIDI (ADR-018). A hand edit is just as good.

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

## Bench test log

Every result recorded, pass or fail, oldest first. A pass that verifies a capability record also
adds a row to the matching table above.

| Test | Date | Outcome | Bench log (`captures/`) | Notes |
|---|---|---|---|---|
| _none yet_ | | | | |
