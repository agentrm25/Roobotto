"""T-03 acceptance: production-equivalent classes computed from the cue sheet.

Two beat positions are equivalent when every way the band could have got there leaves the rig
in the same state, and every way the song could go on asks for the same cues within the
horizon. Cue ids, and the section a cue is written under, do not matter: only what it produces.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from roobotto.arrangement import ArrangementError, BeatPosition, Position, expand
from roobotto.config.song import Song
from roobotto.cues.equivalence import production_classes

ANTHEM = Path(__file__).parents[2] / "examples" / "songs" / "synthetic_anthem.yaml"


def song_from(
    sections: list[dict[str, Any]],
    cues: list[dict[str, Any]] | None = None,
    graph: list[dict[str, Any]] | None = None,
) -> Song:
    return Song.model_validate(
        {
            "kind": "song",
            "schema_version": 0,
            "id": "test_song",
            "title": "Test song",
            "tempo": {"nominal": 120, "range": [100, 140]},
            "meter": "4/4",
            "sections": sections,
            "graph": graph or [],
            "cues": cues or [],
        }
    )


def look(
    cue_id: str,
    section: str,
    scene: str,
    cue_class: str = "standard",
    bar: int = 1,
    beat: int = 1,
    offset: float = 0.0,
    on_miss: str = "send_now",
) -> dict[str, Any]:
    return {
        "id": cue_id,
        "at": {"section": section, "bar": bar, "beat": beat, "offset_beats": offset},
        "class": cue_class,
        "state": {"mvp.scene": scene},
        "on_miss": on_miss,
    }


def bp(section: str, visit: int, bar: int, beat: int) -> BeatPosition:
    return BeatPosition(Position(section, visit, bar), beat)


def same(song: Song, horizon: int, a: BeatPosition, b: BeatPosition) -> bool:
    return production_classes(expand(song), horizon_beats=horizon).equivalent(a, b)


# --- Verse / chorus template -------------------------------------------------------------


def verse_chorus(
    chorus2_scene: str = "chorus",
    chorus2_class: str = "standard",
    chorus2_on_miss: str = "send_now",
) -> Song:
    """verse1 (2 bars), chorus1, verse2 (as verse1), chorus2 (as chorus1), outro."""
    return song_from(
        [
            {"id": "verse1", "bars": 2},
            {"id": "chorus1", "bars": 2},
            {"id": "verse2", "bars": 2, "model_as": "verse1"},
            {"id": "chorus2", "bars": 2, "model_as": "chorus1"},
            {"id": "outro", "bars": 2, "ends": True},
        ],
        cues=[
            look("verse1_look", "verse1", "verse"),
            look("chorus1_look", "chorus1", "chorus"),
            look("verse2_look", "verse2", "verse"),
            look("chorus2_look", "chorus2", chorus2_scene, chorus2_class, on_miss=chorus2_on_miss),
        ],
    )


def test_a_song_without_cues_is_one_class() -> None:
    song = song_from([{"id": "a", "bars": 2}, {"id": "b", "bars": 3, "ends": True}])
    classes = production_classes(expand(song), horizon_beats=4)
    assert len(classes.classes) == 1
    assert classes.classes[0] == expand(song).beat_positions()


def test_identical_verses_are_equivalent_once_their_looks_match() -> None:
    song = verse_chorus()
    # After the verse cue fired, both verses have the rig in "verse" and ask for "chorus" next.
    assert same(song, 8, bp("verse1", 1, 1, 2), bp("verse2", 1, 1, 2))
    assert same(song, 8, bp("verse1", 1, 2, 3), bp("verse2", 1, 2, 3))


def test_the_same_downbeat_differs_because_the_rig_state_differs() -> None:
    # At verse2's first beat the rig still shows chorus1's look; at verse1's it shows nothing.
    assert not same(verse_chorus(), 8, bp("verse1", 1, 1, 1), bp("verse2", 1, 1, 1))


def test_positions_split_only_once_their_cues_come_within_the_horizon() -> None:
    song = verse_chorus(chorus2_scene="chorus_big")
    a, b = bp("verse1", 1, 1, 2), bp("verse2", 1, 1, 2)
    assert same(song, 4, a, b)  # chorus cues are 7 beats ahead: outside a 4-beat horizon
    assert same(song, 7, a, b)  # due at +7 is outside [0, 7)
    assert not same(song, 8, a, b)  # now the horizon sees verse1 -> chorus vs verse2 -> big


@pytest.mark.parametrize(
    "change",
    [
        {"chorus2_scene": "chorus_big"},
        {"chorus2_class": "decorative"},
        {"chorus2_on_miss": "skip"},
    ],
    ids=["state", "class", "on_miss"],
)
def test_what_a_cue_asks_for_matters_not_just_when(change: dict[str, str]) -> None:
    song = verse_chorus(**change)
    assert not same(song, 8, bp("verse1", 1, 1, 2), bp("verse2", 1, 1, 2))


def test_cue_ids_and_owning_sections_do_not_matter() -> None:
    base = verse_chorus()
    renamed = base.model_copy(
        update={"cues": [c.model_copy(update={"id": f"x{i}"}) for i, c in enumerate(base.cues)]}
    )
    assert (
        production_classes(expand(base), horizon_beats=8).classes
        == production_classes(expand(renamed), horizon_beats=8).classes
    )


# --- Anticipating cues and extensions ----------------------------------------------------


def anticipating() -> Song:
    """a (2 bars, may be extended by 1 bar) then b; b's cue fires 2 beats before b starts."""
    return song_from(
        [
            {"id": "a", "bars": 2, "extendable": {"unit": 1, "prob": 0.5}},
            {"id": "b", "bars": 2, "ends": True},
        ],
        cues=[look("b_in", "b", "go", offset=-2.0)],
    )


def test_a_cue_before_a_section_depends_on_whether_the_section_is_reached() -> None:
    song = anticipating()
    # Beat 3 of bar 2 (exit point): leaving fires the cue now; extending does not.
    # Beat 3 of bar 3 (also an exit point, one more bar allowed): the same two futures
    # within a 4-beat horizon. So extending a bar does not change what the band must prepare.
    assert same(song, 4, bp("a", 1, 2, 3), bp("a", 1, 3, 3))
    # At the extension cap the band must leave, so there is no "extend" future left.
    assert not same(song, 4, bp("a", 1, 2, 3), bp("a", 1, 4, 3))


def test_a_cue_due_before_the_performance_starts_never_fires() -> None:
    song = song_from(
        [{"id": "a", "bars": 2}, {"id": "b", "bars": 2, "ends": True}],
        cues=[
            look("too_early", "a", "x", offset=-4.0),  # due 4 beats before the song starts
            look("in_b", "b", "x"),
        ],
    )
    # If "too_early" had fired, the rig would show x from the first beat, just like after in_b.
    assert not same(song, 1, bp("a", 1, 1, 1), bp("b", 1, 1, 2))


# --- Template with repeated visits -------------------------------------------------------


def test_repeated_visits_are_equivalent_until_something_tells_them_apart() -> None:
    song = Song.model_validate(yaml.safe_load(ANTHEM.read_text(encoding="utf-8")))
    # Nothing in chorus3 or just after it asks for anything, so any visit looks the same.
    assert same(song, 8, bp("chorus3", 1, 3, 1), bp("chorus3", 2, 3, 1))
    assert same(song, 8, bp("chorus3", 2, 3, 1), bp("chorus3", 3, 3, 1))


def test_the_last_visit_splits_off_when_the_exit_cue_is_near() -> None:
    data = yaml.safe_load(ANTHEM.read_text(encoding="utf-8"))
    data["cues"].append(look("finale", "outro", "finale"))
    song = Song.model_validate(data)
    last_beat = (8, 4)
    first = bp("chorus3", 1, *last_beat)
    second = bp("chorus3", 2, *last_beat)
    third = bp("chorus3", 3, *last_beat)
    # Visits 1 and 2 may repeat or go on to the finale; visit 3 can only go on to it.
    assert same(song, 4, first, second)
    assert not same(song, 4, second, third)


# --- The result object -------------------------------------------------------------------


def test_every_beat_position_of_the_anthem_is_in_exactly_one_class() -> None:
    song = Song.model_validate(yaml.safe_load(ANTHEM.read_text(encoding="utf-8")))
    grid = expand(song)
    classes = production_classes(grid, horizon_beats=8)
    members = [m for cls in classes.classes for m in cls]
    assert sorted(members, key=grid.beat_positions().index) == list(grid.beat_positions())
    assert len(set(members)) == len(members) == len(grid.positions) * grid.beats_per_bar


def test_class_of_agrees_with_the_class_list() -> None:
    song = verse_chorus()
    classes = production_classes(expand(song), horizon_beats=8)
    for index, cls in enumerate(classes.classes):
        assert all(classes.class_of(m) == index for m in cls)


def test_classes_come_out_in_play_order() -> None:
    song = verse_chorus()
    grid = expand(song)
    order = {b: i for i, b in enumerate(grid.beat_positions())}
    classes = production_classes(grid, horizon_beats=8)
    firsts = [order[cls[0]] for cls in classes.classes]
    assert firsts == sorted(firsts)
    assert all([order[m] for m in cls] == sorted(order[m] for m in cls) for cls in classes.classes)


def test_a_position_outside_the_grid_is_an_error() -> None:
    classes = production_classes(expand(verse_chorus()), horizon_beats=8)
    with pytest.raises(KeyError):
        classes.class_of(bp("verse1", 1, 9, 1))


def test_the_horizon_must_be_positive() -> None:
    with pytest.raises(ValueError, match="horizon_beats"):
        production_classes(expand(verse_chorus()), horizon_beats=0)


def test_too_many_paths_fails_loudly() -> None:
    with pytest.raises(ArrangementError, match="possible paths"):
        production_classes(expand(verse_chorus()), horizon_beats=8, max_paths=0)
