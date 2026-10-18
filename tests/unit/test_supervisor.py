"""Output supervisor: routing, arming, dedup, rate limits, recipients (I-1, I-2, I-5, I-7, I-11,
I-13, I-15, I-18)."""

from typing import Any

import pytest

from roobotto.adapters.midi import MidiAdapter
from roobotto.adapters.mock import MockAdapter
from roobotto.config.rig import Listen, Rig
from roobotto.cues.ledger import FiredLedger
from roobotto.cues.supervisor import Commit, Intent, Refusal, Supervisor
from roobotto.messages import Authorization, FiredLedgerRecord, OccurrenceId, OutputAttempt
from spine_support import MS, RecordingBackend, rig

HASH = "c" * 64
GO = Authorization(event_id="e1", action="go")


def occ(cue: str = "chorus_synth", visit: int = 1) -> OccurrenceId:
    return OccurrenceId(performance="p", song_instance=0, section_visit=visit, cue_id=cue)


def commit(*intents: tuple[str, str, str], cue: str = "chorus_synth", **over: Any) -> Commit:
    base: dict[str, Any] = {
        "occurrence": occ(cue),
        "generation": 0,
        "cue_class": "standard",
        "intents": tuple(Intent(*i) for i in intents),
        "reason": "test",
        "window_end_ns": 10**12,
        "authorized_by": GO,
    }
    return Commit(**(base | over))


def make(
    the_rig: Rig, *, backend: bool = False
) -> tuple[Supervisor, FiredLedger, RecordingBackend]:
    rec = RecordingBackend()
    ledger = FiredLedger()
    sup = Supervisor(
        the_rig,
        ledger,
        mock=MockAdapter(),
        midi=MidiAdapter(rec if backend else None),
        config_hash=HASH,
    )
    return sup, ledger, rec


def submit(
    sup: Supervisor, c: Commit, *, now: int = 10**9, armed: bool = False, stopped: bool = False
) -> tuple[list[OutputAttempt], list[Refusal]]:
    result = sup.submit(c, now_ns=now, generation=0, stopped=stopped, armed=armed)
    return result.attempts, result.refusals


SYNTH = ("synth", "patch", "bright")


def _unverified_physical() -> Rig:
    """A rig the validator would reject: a 'documented' command on a physical port."""
    physical = rig("physical")
    synth = physical.device("synth")
    assert synth is not None
    bad_cmd = synth.commands["patch"].model_copy(update={"status": "documented"})
    bad_dev = synth.model_copy(update={"commands": {"patch": bad_cmd}})
    devices = [bad_dev if d.id == "synth" else d for d in physical.devices]
    return physical.model_copy(update={"devices": devices})


@pytest.mark.parametrize(
    ("the_rig", "armed", "backend", "adapter", "route"),
    [
        (rig("mock"), True, True, "mock", "mock port"),
        (rig("physical"), False, True, "mock", "not armed"),
        (rig("physical"), True, False, "mock", "MIDI adapter disabled"),
        (rig("physical"), True, True, "midi", "armed and bench_verified"),
        (_unverified_physical(), True, True, "mock", "no override (I-1)"),
    ],
    ids=["mock-profile", "unarmed", "adapter-disabled", "verified-armed", "unverified-no-override"],
)
def test_routing(the_rig: Rig, armed: bool, backend: bool, adapter: str, route: str) -> None:
    sup, _, rec = make(the_rig, backend=backend)
    attempts, refusals = submit(sup, commit(SYNTH), armed=armed)
    assert refusals == []
    [attempt] = attempts
    assert attempt.adapter == adapter and route in attempt.reason
    assert attempt.armed is armed and attempt.config_hash == HASH
    assert attempt.bytes_sent == [0xC1, 5]
    assert rec.sent == ([("usb_out", bytes([0xC1, 5]))] if adapter == "midi" else [])


def test_ledger_written_before_send_and_blocks_a_second_send() -> None:
    sup, ledger, _ = make(rig())
    attempts, _ = submit(sup, commit(SYNTH))
    assert len(attempts) == 1
    assert [r.result for r in ledger.records() if isinstance(r, FiredLedgerRecord)] == [
        "attempting",
        "attempted",
    ]
    attempts, refusals = submit(sup, commit(SYNTH), now=10**10)
    assert attempts == [] and "fired ledger" in refusals[0].reason


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [({"stopped": True}, "STOP"), ({}, "stale generation")],
)
def test_stop_and_stale_generation_send_nothing(kwargs: dict[str, Any], reason: str) -> None:
    sup, ledger, _ = make(rig())
    c = commit(SYNTH) if kwargs else commit(SYNTH, generation=7)
    attempts, refusals = submit(sup, c, **kwargs)
    assert attempts == [] and reason in refusals[0].reason and ledger.records() == ()


def test_resync_resends_state_only_and_never_critical() -> None:
    sup, _, _ = make(rig())
    submit(sup, commit(SYNTH))
    attempts, _ = submit(sup, commit(SYNTH, resync=True), now=10**10)
    assert len(attempts) == 1
    one_shot = commit(("mvp", "scene", "chorus"), cue="chorus_scene", resync=True)
    attempts, refusals = submit(sup, one_shot, now=2 * 10**10)
    assert attempts == [] and "never resent" in refusals[0].reason
    critical = commit(
        ("guitar", "snapshot", "lead"), cue="solo_lead", cue_class="critical", resync=True
    )
    attempts, refusals = submit(sup, critical, now=3 * 10**10)
    assert attempts == [] and "critical" in refusals[0].reason


def test_rate_limit_per_device_command() -> None:
    sup, _, _ = make(rig())
    submit(sup, commit(SYNTH), now=10**9)
    attempts, refusals = submit(sup, commit(SYNTH, cue="other"), now=10**9 + 100 * MS)
    assert attempts == [] and "rate limit" in (refusals[0].reason)
    attempts, _ = submit(sup, commit(SYNTH, cue="third"), now=10**9 + 250 * MS)
    assert len(attempts) == 1


def test_i13_runtime_recipient_check() -> None:
    """Even if a rig slipped past validation, a listener with unknown behavior blocks the send."""
    base = rig()
    mvp = base.device("mvp")
    assert mvp is not None
    listener = mvp.model_copy(
        update={
            "id": "bystander",
            "receives": [Listen(port="mock_out_synth", channel=2, behavior="unknown")],
            "commands": {},
        }
    )
    unsafe = base.model_copy(update={"devices": [*base.devices, listener]})
    sup, ledger, _ = make(unsafe)
    attempts, refusals = submit(sup, commit(SYNTH))
    assert attempts == [] and "I-13" in refusals[0].reason and ledger.records() == ()


def test_disconnected_port_queues_nothing() -> None:
    sup, _, _ = make(rig())
    sup.set_port("mock_out_synth", False)
    attempts, refusals = submit(sup, commit(SYNTH))
    assert attempts == [] and "disconnected" in refusals[0].reason
    sup.set_port("mock_out_synth", True)
    attempts, _ = submit(sup, commit(SYNTH, cue="later"), now=10**10)
    assert len(attempts) == 1


@pytest.mark.parametrize(
    ("intent", "reason"),
    [
        (("nope", "scene", "x"), "not in the rig"),
        (("mvp", "nope", "x"), "no command"),
        (("mvp", "scene", "nope"), "not a value"),
    ],
)
def test_unknown_targets_are_refused(intent: tuple[str, str, str], reason: str) -> None:
    sup, _, _ = make(rig())
    attempts, refusals = submit(sup, commit(intent))
    assert attempts == [] and reason in refusals[0].reason


def test_performer_owned_device_is_refused() -> None:
    base = rig()
    mvp = base.device("mvp")
    assert mvp is not None
    owned = base.model_copy(
        update={
            "devices": [
                mvp.model_copy(update={"owner": "performer"}) if d.id == "mvp" else d
                for d in base.devices
            ]
        }
    )
    sup, _, _ = make(owned)
    attempts, refusals = submit(sup, commit(("mvp", "scene", "chorus")))
    assert attempts == [] and "owned by the performer" in refusals[0].reason


def test_i13_target_must_list_itself_as_intended() -> None:
    base = rig()
    synth = base.device("synth")
    assert synth is not None
    deaf = base.model_copy(
        update={
            "devices": [
                synth.model_copy(update={"receives": []}) if d.id == "synth" else d
                for d in base.devices
            ]
        }
    )
    sup, _, _ = make(deaf)
    attempts, refusals = submit(sup, commit(SYNTH))
    assert attempts == [] and "intended input" in refusals[0].reason
