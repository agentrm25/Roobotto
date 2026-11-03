# TO-T-07 — 10-minute capture validation with a shared marker signal

| Field | Value |
|---|---|
| Serves | [T-07 Recorder](../implementation-plan.md#appendix-b--phase-0-task-cards) done-when 1 and 3 (10-minute alignment within 1 ms after correction, or the measured error; a clean run drops no buffers); plan Section 9 "Validate the topology first"; day-14 item 4 |
| Revision | 1 (2026-11-03) |
| Estimated time | 45 minutes, plus 20 minutes setup |
| People needed | Randy only |
| Location | Home or bench: the Mac, the Helix (USB audio), and whichever second interface the rehearsal capture will use |

## 1. Purpose
Prove that the capture topology you will use at the rehearsal (one device, or an Aggregate
Device of two) keeps every input aligned within 1 ms over 10 minutes after Roobotto's linear
correction, or measure by how much it does not. The same recording shows whether a clean run
drops buffers. Until this passes, no rehearsal take is trusted (plan Section 9).

## 2. Safety
- **NOTE** Nothing is sent to any device. The recorder only listens: audio in, MIDI in. No cue,
  no preset change, no output.
- **CAUTION** Do not patch the marker click into the band's live signal paths. Use spare inputs,
  or do this at home with the rig off.
- **NOTE** Keep the click at a comfortable level; it only needs to be clearly above the noise
  floor on every input.

## 3. Prerequisites
- **Equipment:** the Mac; the Helix connected by USB; the second interface if the rehearsal will
  use one; a click source that can be split to every input you will record (a metronome app on a
  phone into a small mixer with several outputs, or a Y-splitter cable set); cables to reach one
  input per device.
- **Software:** macOS; Roobotto on `main` after this TO's PR merges; `uv sync` done;
  `roobotto doctor` reads ready (TO-T-06 executed).
- **Prior TOs:** TO-T-06 (`local/ports.yaml` exists; `roobotto devices list` shows the devices).
- **Backups:** none; nothing on any device changes.

## 4. Initial state
- If two devices: in Audio MIDI Setup, create an Aggregate Device with both. Note which device
  is the clock source and whether Drift Correction is ticked for the other: `RECORD → aggregate
  device`, `RECORD → clock source`, `RECORD → drift correction members`.
- Click source running at one click every 10 seconds (or every 5), split so the same click
  reaches one input on each device. If only one device: two inputs on it.
- Write `local/sessions/marker-test.yaml` (private) from this skeleton, with the device names
  exactly as `roobotto devices list` prints them and the channel numbers you cabled:

```yaml
kind: session
schema_version: 0
id: marker_test_1
song: synthetic_anthem          # any song id; this take is not musical
take_type: bench
sample_rate: 48000
bit_depth: 24
block: 512
devices:
  - { id: helix, name: "Helix Stadium", channels: { 6: guitar } }   # USB 7 is index 6
  - { id: iface, name: "YOUR SECOND INTERFACE", channels: { 0: room } }
midi_ports: all
clock:
  aggregate_device: "YOUR AGGREGATE NAME"
  clock_source: "Helix Stadium"
  drift_correction: ["YOUR SECOND INTERFACE"]
consent: none                   # bench take: no performers
```
If the Aggregate Device appears as one device to the Mac, list it once with both channel
indices instead.

## 5. Procedure
1. **Do:** in the Roobotto folder run
   ```
   uv run roobotto devices list
   ```
   **Expect:** the device names you wrote in the session file appear under audio devices, 48000
   Hz. **If not:** fix the names in the file; replug; rerun.
2. **Do:** run
   ```
   uv run roobotto record session local/sessions/marker-test.yaml --out recordings/bench/marker-test-1 --seconds 600
   ```
   **Expect:** `recording to recordings/bench/marker-test-1: ...`, then `sync burst now (start)`.
   The click keeps running for the whole 10 minutes. **If not:** copy the error and stop
   (Section 7).
3. **Do:** when it prints `sync burst now (end)` and `wrote ...`, read the line: `RECORD →
   flagged blocks` (must be 0 for a clean run), `RECORD → frames per stream`.
4. **Do:** run
   ```
   uv run roobotto record validate recordings/bench/marker-test-1 --reference guitar
   ```
   **Expect:** one line per stream with markers (about 60), offset, drift in ppm, residual max
   and raw max in ms, and PASS or FAIL. `RECORD → the whole output`.
   **If FAIL:** that is a result, not a mistake. Record it and continue.
5. **Do:** run
   ```
   uv run roobotto record correct recordings/bench/marker-test-1
   uv run roobotto record validate recordings/bench/marker-test-1/corrected --reference guitar
   ```
   **Expect:** the corrected copy's raw max is within 1 ms. `RECORD → corrected result`.
6. **Do (injected discontinuity, optional but valuable):** repeat step 2 with `--seconds 60` into
   `recordings/bench/marker-test-2`, and at about 30 s unplug and replug the second interface's
   USB cable (or stop the click on one input for 5 s). Run `record validate` on it.
   **Expect:** an invalid interval around the moment you did it. `RECORD → interval reported`.

## 6. Data capture
- The take folders stay under `recordings/` (ignored by git).
- Copy `alignment.json` from each take and the validate output into
  `captures/<date>-capture/` as the bench log.

| Field | Value |
|---|---|
| aggregate device / clock source / drift correction | |
| flagged blocks (step 3) | |
| frames per stream | |
| validate output (step 4) | |
| corrected result (step 5) | |
| interval reported (step 6) | |

## 7. Restore and teardown
Unpatch the click. Delete the Aggregate Device only if the rehearsal will not use it. Nothing
else changed.

## 8. Sign-off
| Item | Value |
|---|---|
| Executed by / date | |
| All steps completed? (list skipped steps and why) | |
| Pass / fail against the acceptance check | |
| Anomalies or surprises | |
| Bench log path | |

## 9. Hand-back to the agent
Give the agent `captures/<date>-capture/` (alignment.json files and the pasted outputs; no
audio). The agent then updates `docs/evidence-register.md` (E-41 to E-43 from assumed to
confirmed or corrected), the T-07 card's bench checks, and, if the residual exceeded 1 ms, opens
the topology question in Section 10.3 of the plan.
