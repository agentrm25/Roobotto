# TO-T-06 — First run of `doctor` and `devices list` on the Mac

| Field | Value |
|---|---|
| Serves | [T-06 Device tools](../implementation-plan.md#appendix-b--phase-0-task-cards) (P0 done-when: "Randy runs them on the bench"); prepares R-2, H-6 and M-1 |
| Revision | 1 (2026-11-02) |
| Estimated time | 20 minutes, plus 5 minutes setup |
| People needed | Randy only |
| Location | Bench (the Mac, the H4, and whatever MIDI devices are normally plugged in) |

## 1. Purpose
Prove that Roobotto's device tools see the Mac's MIDI ports and audio devices, produce a
filled-in `local/ports.yaml`, and that `roobotto doctor` reports "ready" on a mock profile. This
closes the T-06 P0 check "Randy runs them on the bench" for `devices list` and `doctor`, and gives
the agent the real port names that R-2 and M-1 will use.

## 2. Safety
- **NOTE** Nothing in this TO sends MIDI or plays audio. `devices list` and `doctor` only read
  names. The rig stays in the mock profile throughout.
- **CAUTION** Do not save over any Helix or H4 preset during this TO. No preset changes are
  needed.
- **NOTE** Volume can stay where it is; no audio passes through Roobotto.

## 3. Prerequisites
- **Equipment:** the Mac; the H4 connected by USB-C as it normally is; the Helix and any other
  MIDI devices connected and powered as for a rehearsal (so their port names appear). Nothing
  else.
- **Software:** macOS; Roobotto on branch `main` after this TO's PR merges; Terminal.
  Pre-run, in the Roobotto folder:
  ```
  uv sync
  ```
  **Expect:** no errors. **If not:** paste the output to the agent and stop.
- **Prior TOs:** none.
- **Backups:** none needed; nothing is written outside `local/`.

## 4. Initial state
- Mac on, Roobotto folder open in Terminal (`cd` into it).
- H4, Helix, KeyLab (if you have it on the bench) connected and powered. Note which ones are
  connected: `RECORD → devices connected`.
- No Roobotto process running.

## 5. Procedure
1. **Do:** run
   ```
   mkdir -p local captures
   ```
   **Expect:** no output. **If not:** check you are in the Roobotto folder (`ls` shows
   `AGENTS.md`).
2. **Do:** run
   ```
   uv run roobotto ui --no-browser examples
   ```
   then open the printed address (`http://127.0.0.1:8765/`) in Safari or Chrome and click
   **Doctor & devices**.
   **Expect:** a list of checks, a "What this machine sees" card listing MIDI inputs, MIDI
   outputs and audio devices, and a "Ports file for this machine" card.
   **If not:** copy whatever the page or the Terminal shows and go to Section 7.
3. **Do:** compare the MIDI lists with what is plugged in. Every connected device should appear
   under inputs and/or outputs with the H4 showing one entry per port.
   **Expect:** H4 ports and the Helix are listed. `RECORD → MIDI inputs seen`,
   `RECORD → MIDI outputs seen` (copy the exact names).
   **If not:** unplug and replug the missing device, reload the page. If it still does not
   appear, note it and continue; the agent will look at it.
4. **Do:** read the audio devices table. `RECORD → audio devices seen` (name, in/out counts,
   rate). The Helix should show its USB channels at 48000 Hz (H-6 needs this later).
   **Expect:** the Helix appears. **If not:** note the "Audio library" check's message.
5. **Do:** click **Copy to clipboard** on the ports card, open TextEdit (plain text: Format >
   Make Plain Text), paste, and save as `local/ports.yaml` inside the Roobotto folder.
   **Expect:** a file starting with `# Ports for rig 'rig_template'`.
   **If not:** select the text in the card by hand and copy it.
6. **Do:** leave every value `null` for now. The template rig uses mock ports, which need no
   system names; R-2 will add the real physical ports later.
7. **Do:** in a second Terminal tab, run
   ```
   uv run roobotto doctor examples local/ports.yaml
   ```
   **Expect:** every line is `[ok  ]` or `[warn]`, and the last line is "All checks passed." or
   "Ready, with warnings." `RECORD → doctor output` (copy all of it).
   **If not:** the failing line names a fix. Try it once; if it still fails, go to Section 7.
8. **Do:** stop the workbench with Ctrl-C in the first Terminal tab.

## 6. Data capture
- Save the doctor output as `captures/2026-11-02-doctor/doctor.txt` (adjust the date).
- Save `uv run roobotto devices list --json` output as `captures/<date>-doctor/devices.json`.
- `local/ports.yaml` stays in `local/` (private; not sent to the agent unless you choose to).

| Field | Value |
|---|---|
| devices connected | |
| MIDI inputs seen | |
| MIDI outputs seen | |
| audio devices seen | |
| doctor output | (file path) |

## 7. Restore and teardown
Nothing was changed on any device. Close the workbench (Ctrl-C). The rig is show-ready as it
was.

## 8. Sign-off
| Item | Value |
|---|---|
| Executed by / date | |
| All steps completed? (list skipped steps and why) | |
| Pass / fail against the acceptance check | |
| Anomalies or surprises | |
| Bench log path | |

## 9. Hand-back to the agent
Give the agent: the filled-in table above, `captures/<date>-doctor/doctor.txt` and
`devices.json` (both contain only port and device names). The agent then updates
`docs/evidence-register.md` (E-27 to E-29 move from assumed to confirmed or corrected) and the
T-06 card's bench check, and writes TO-R-2 using the real port names.
