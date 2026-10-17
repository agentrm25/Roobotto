# Safety model

> Stub. The contracts are defined in `implementation-plan.md` Section 5 and ADR-009 to ADR-014. This file will become the standalone reference as the control spine (T-04) lands.

Summary of the non-negotiables (see `AGENTS.md`):

- Physical outputs are off by default; arming is explicit and never persists.
- Only performer-intent events authorize GO, BACK, or critical cues; everything else fails closed.
- Tracker evidence alone never fires a critical cue, in any mode or milestone.
- One-shots fire at most once per occurrence, via the fired ledger.
- STOP halts Roobotto's output instantly. RESTORE (H4 preset A) is a separate operation.

Invariants I-1 to I-19 are tested in `tests/property/test_invariants.py`.
