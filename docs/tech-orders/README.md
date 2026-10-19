# Tech Orders

A **Tech Order (TO)** is a step-by-step procedure for work Randy does outside the coding
environment: setting up or rewiring equipment, bench verification (Section 10.2), recording
sessions, latency captures, annotation, device settings, purchases, or anything physical.

## The rule

Every task card, bench test (H-, M-, A-, K-, R-, P-, L-) or agent request that needs
real-world action from Randy **must ship with its own Tech Order** in this folder. The agent
writes it; Randy executes it. No TO, no request.

- One TO per real-world activity. Don't bury physical steps inside a PR description, a task
  card, or chat.
- The TO is written **before** Randy is asked to do the work, and is linked from the card or PR.
- A card whose acceptance depends on a TO stays open until the TO's sign-off record is filled
  in and its outputs exist. The agent never marks those checks done on assumption.
- TOs never instruct Roobotto to send to real hardware with an unverified command. First-time
  verification uses external tools (MIDI Monitor, SendMIDI, an existing controller).
- TOs are committed; their **results** are not. Filled-in logs go to `captures/` (bench) or
  `local/` (private show data), never into this folder.

## Naming

`TO-<id>-<short-slug>.md`, where `<id>` is the bench test or task card it serves.
Examples: `TO-H-1-helix-snapshot-reports.md`, `TO-T-07-capture-marker-test.md`.
Revise in place and bump the revision number; don't create copies.

## Index

| TO | Title | Serves | Rev | Status |
|---|---|---|---|---|
| _none yet_ | | | | |

Status values: `draft` · `ready` · `executed` · `superseded`.

## Required template

Copy this skeleton. Every section is mandatory; write "None" rather than deleting one.

```markdown
# TO-<id> — <Title>

| Field | Value |
|---|---|
| Serves | <task card / bench test ID(s), with links> |
| Revision | 1 (<date>) |
| Estimated time | <minutes>, plus <setup/teardown minutes> |
| People needed | <Randy only / Randy + bandmate / full band> |
| Location | <bench / rehearsal room / venue> |

## 1. Purpose
One or two sentences: what this proves or produces, and which acceptance check it unblocks.

## 2. Safety
Use these labels, before the step they apply to:
- **WARNING** — risk of injury, hearing damage, or equipment damage.
- **CAUTION** — risk of a wrong cue, lost data, or a changed live preset.
- **NOTE** — useful context.
Always cover: volume levels, lighting state, which presets are live-show presets and must not
be saved over, and how to back out.

## 3. Prerequisites
- **Equipment:** every device, cable (type and length), adapter, and power supply.
- **Software:** apps and versions, Roobotto commit/branch, any CLI command to pre-run.
- **Prior TOs:** which must be `executed` first.
- **Backups:** what to back up first (e.g. Helix presets, H4 config) and how.

## 4. Initial state
The exact starting configuration: what is connected where, which preset/snapshot is
selected, which settings are on/off. Include a wiring diagram or table.

## 5. Procedure
Numbered steps. Each step has exactly one action and states:
1. **Do:** the action (menu path, button, cable, command — copy-pasteable).
   **Expect:** what you should see or hear.
   **If not:** what to do (retry, check X, or stop and go to Section 7).
Mark checkpoints where Randy records a value: `RECORD → <field name>`.

## 6. Data capture
- What to save, file names, and the destination folder (`captures/<date>-<device>/` or
  `local/...`).
- A blank results table matching every `RECORD →` field above.

## 7. Restore and teardown
Steps to return the rig to its show-ready state, and how to confirm it (e.g. "load preset A,
play song 1 snapshot cues, all behave as before").

## 8. Sign-off
| Item | Value |
|---|---|
| Executed by / date | |
| All steps completed? (list skipped steps and why) | |
| Pass / fail against the acceptance check | |
| Anomalies or surprises | |
| Bench log path | |

## 9. Hand-back to the agent
Exactly what Randy gives the agent afterwards (files, pasted values, photos) and which docs
the agent then updates (`hardware-verification.md`, `evidence-register.md`, capability
records).
```

## Writing standards

- Write for someone tired, at a gig-volume rehearsal, with one hand on a guitar.
- No ambiguity: name the exact menu path, port, cable end, and value. Never "configure X".
- State every assumption about Randy's rig and mark it `[verify]` in the TO and in the
  evidence register.
- Keep TOs self-contained: link to the plan for background, but the steps must be executable
  without reading it.
