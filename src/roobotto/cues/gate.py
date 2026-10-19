"""The decision gate: a pure function of (posterior, cue spec, device state, now) plus the
authority context (plan Sections 5.1, 5.4; ADR-011, ADR-013).

Rules, in order:

1. STOP is active: abstain.
2. ``now`` is past the cue's ``on_miss`` window: abstain (I-4).
3. A target device is unknown, owned by the performer, or changed by the performer in this
   visit: abstain (I-12, one owner per device).
4. With a performer authorization for this occurrence: fire. A critical cue is only ever given
   an authorization when its ``requires.corroborate_by`` lists the kind of action that made it.
5. Without one, a critical cue is refused, in every mode and milestone and whatever the posterior
   (ADR-013, I-14). ``on_uncertain`` decides whether that refusal asks the performer.
6. Tracker-only decorative and standard cues fire only in ASSISTED or AUTO, with tracker
   authority enabled and fresh, from M4 (decorative) or M5 (standard), and when the posterior
   reaches the cue's threshold.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from roobotto.config.song import Cue
from roobotto.messages import Authorization, Decision, OccurrenceId

Mode = Literal["manual", "assisted", "auto"]
TimingClass = Literal["Q", "D", "L", "F"]

DEFAULT_THRESHOLD = {"decorative": 0.33, "standard": 0.8}
"""C_FP / (C_FP + C_FN) from Section 5.1, used when ``requires.posterior_min`` is not set."""
TRACKER_MILESTONE = {"decorative": 4, "standard": 5}
"""The milestone from which the tracker may fire each class (Section 5.1). Never critical."""


@dataclass(frozen=True)
class DeviceView:
    owner: Literal["roobotto", "performer", "missing"]
    overridden: bool = False
    """The performer changed this device during the current visit (I-12)."""


@dataclass(frozen=True)
class GateInput:
    posterior: float | None
    cue: Cue
    devices: Mapping[str, DeviceView]
    now_ns: int
    occurrence: OccurrenceId
    window_end_ns: int
    authorization: Authorization | None
    mode: Mode
    milestone: int
    tracker_authority: bool
    tracker_fresh: bool
    stopped: bool
    timing_class: TimingClass | None = None


def decide(g: GateInput) -> Decision:
    def out(
        kind: Literal["fire", "hold", "ask", "abstain"], reason: str, *evidence: str
    ) -> Decision:
        return Decision(
            kind=kind,
            reason=reason,
            evidence=list(evidence),
            occurrence=g.occurrence,
            go_timing_class=g.timing_class,
        )

    cue = g.cue
    if g.stopped:
        return out("abstain", "STOP is active (I-18)")
    if g.now_ns > g.window_end_ns:
        return out("abstain", "expired: past the on_miss window (I-4)")
    for device_id, view in sorted(g.devices.items()):
        if view.owner == "missing":
            return out("abstain", f"device '{device_id}' is not in the rig")
        if view.owner != "roobotto":
            return out("abstain", f"device '{device_id}' is owned by the performer")
        if view.overridden:
            return out(
                "abstain", f"the performer changed '{device_id}' in this visit; they win (I-12)"
            )

    if g.authorization is not None:
        return out(
            "fire",
            f"authorized by performer {g.authorization.action.upper()} "
            f"'{g.authorization.event_id}'",
            f"authorization:{g.authorization.event_id}",
        )

    if cue.cue_class == "critical":
        return out(
            cue.on_uncertain,
            "refused: a critical cue needs a performer action that authorizes this occurrence; "
            "tracker evidence alone never fires it (ADR-013, I-14)",
        )
    if g.mode == "manual":
        return out("abstain", "MANUAL: the tracker may display but not fire")
    if not g.tracker_authority:
        return out("abstain", "tracker authority is suspended")
    if not g.tracker_fresh:
        return out("abstain", "no fresh tracker state (I-8)")
    needed = TRACKER_MILESTONE[cue.cue_class]
    if g.milestone < needed:
        return out(
            "abstain",
            f"the tracker may fire {cue.cue_class} cues from M{needed} (now M{g.milestone})",
        )
    threshold = (
        cue.requires.posterior_min
        if cue.requires and cue.requires.posterior_min is not None
        else DEFAULT_THRESHOLD[cue.cue_class]
    )
    if g.posterior is None or g.posterior < threshold:
        return out(
            cue.on_uncertain,
            f"posterior {g.posterior} is below the threshold {threshold}",
            f"posterior:{g.posterior}",
        )
    return out(
        "fire",
        f"tracker posterior {g.posterior} reaches {threshold}",
        f"posterior:{g.posterior}",
    )
