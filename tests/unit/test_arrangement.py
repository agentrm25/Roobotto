"""T-03 acceptance: a song expands into a bar grid of section visits, bars and successors."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from roobotto.arrangement import ArrangementError, BeatPosition, Position, expand
from roobotto.config.song import Song

ANTHEM = Path(__file__).parents[2] / "examples" / "songs" / "synthetic_anthem.yaml"


def song_from(
    sections: list[dict[str, Any]],
    graph: list[dict[str, Any]] | None = None,
    cues: list[dict[str, Any]] | None = None,
    meter: str = "4/4",
) -> Song:
    return Song.model_validate(
        {
            "kind": "song",
            "schema_version": 0,
            "id": "test_song",
            "title": "Test song",
            "tempo": {"nominal": 120, "range": [100, 140]},
            "meter": meter,
            "sections": sections,
            "graph": graph or [],
            "cues": cues or [],
        }
    )


def anthem() -> Song:
    return Song.model_validate(yaml.safe_load(ANTHEM.read_text(encoding="utf-8")))


def pos(section: str, visit: int, bar: int) -> Position:
    return Position(section, visit, bar)


def targets(song: Song, at: Position, **kwargs: int) -> dict[Position | None, float]:
    return {t.to: t.prob for t in expand(song, **kwargs).successors(at)}


# --- A plain song ------------------------------------------------------------------------


def linear() -> Song:
    return song_from(
        [
            {"id": "verse", "bars": 2},
            {"id": "chorus", "bars": 3},
            {"id": "outro", "bars": 1, "ends": True},
        ]
    )


def test_linear_song_has_one_cell_per_bar_in_play_order() -> None:
    grid = expand(linear())
    assert grid.start == pos("verse", 1, 1)
    assert grid.positions == (
        pos("verse", 1, 1),
        pos("verse", 1, 2),
        pos("chorus", 1, 1),
        pos("chorus", 1, 2),
        pos("chorus", 1, 3),
        pos("outro", 1, 1),
    )


def test_linear_song_successors_are_certain() -> None:
    song = linear()
    assert targets(song, pos("verse", 1, 1)) == {pos("verse", 1, 2): 1.0}
    assert targets(song, pos("verse", 1, 2)) == {pos("chorus", 1, 1): 1.0}
    assert targets(song, pos("outro", 1, 1)) == {None: 1.0}


def test_transition_kinds_tell_the_tracker_what_kind_of_move_it_is() -> None:
    grid = expand(linear())
    assert [t.kind for t in grid.successors(pos("verse", 1, 1))] == ["bar"]
    assert [t.kind for t in grid.successors(pos("verse", 1, 2))] == ["next"]
    assert [t.kind for t in grid.successors(pos("outro", 1, 1))] == ["end"]


def test_linear_song_has_exactly_one_path() -> None:
    grid = expand(linear())
    paths = list(grid.paths())
    assert len(paths) == 1
    assert paths[0].positions == grid.positions
    assert paths[0].prob == 1.0


def test_beat_positions_cover_every_beat_of_every_bar() -> None:
    grid = expand(song_from([{"id": "a", "bars": 2, "ends": True}], meter="3/4"))
    assert grid.beats_per_bar == 3
    assert grid.beat_positions() == tuple(
        BeatPosition(pos("a", 1, bar), beat) for bar in (1, 2) for beat in (1, 2, 3)
    )


# --- The synthetic anthem: extend and repeat loops ---------------------------------------


def test_anthem_grid_size() -> None:
    # intro 4 + verse1 8 + chorus1 8 + verse2 8 + chorus2 8
    # + solo 8 bars plus up to two 4-bar extensions + chorus3 8 bars x 3 visits + outro 4
    assert len(expand(anthem()).positions) == 4 + 8 + 8 + 8 + 8 + 16 + 24 + 4


def test_extendable_solo_runs_longer_in_the_same_visit() -> None:
    song = anthem()
    assert targets(song, pos("solo", 1, 4)) == {pos("solo", 1, 5): 1.0}
    assert targets(song, pos("solo", 1, 8)) == {
        pos("solo", 1, 9): pytest.approx(0.3),
        pos("chorus3", 1, 1): pytest.approx(0.7),
    }
    assert targets(song, pos("solo", 1, 12)) == {
        pos("solo", 1, 13): pytest.approx(0.3),
        pos("chorus3", 1, 1): pytest.approx(0.7),
    }
    # An extension is not a new visit, so the bars before it are not exit points.
    assert targets(song, pos("solo", 1, 10)) == {pos("solo", 1, 11): 1.0}


def test_repeated_chorus_starts_a_new_visit_until_the_declared_maximum() -> None:
    song = anthem()
    assert targets(song, pos("chorus3", 1, 8)) == {
        pos("chorus3", 2, 1): pytest.approx(0.2),
        pos("outro", 1, 1): pytest.approx(0.8),
    }
    assert targets(song, pos("chorus3", 2, 8)) == {
        pos("chorus3", 3, 1): pytest.approx(0.2),
        pos("outro", 1, 1): pytest.approx(0.8),
    }
    # repeat.max is 2 extra times, so visit 3 is the last: the probability goes to the outro.
    assert targets(song, pos("chorus3", 3, 8)) == {pos("outro", 1, 1): pytest.approx(1.0)}


def test_the_extension_cap_is_reported_but_a_declared_maximum_is_not() -> None:
    grid = expand(anthem())
    assert targets(anthem(), pos("solo", 1, 16)) == {pos("chorus3", 1, 1): pytest.approx(1.0)}
    assert grid.truncated == frozenset({pos("solo", 1, 16)})


def test_max_extensions_zero_removes_the_extension_bars() -> None:
    grid = expand(anthem(), max_extensions=0)
    assert pos("solo", 1, 9) not in grid.positions
    assert targets(anthem(), pos("solo", 1, 8), max_extensions=0) == {
        pos("chorus3", 1, 1): pytest.approx(1.0)
    }
    assert grid.truncated == frozenset({pos("solo", 1, 8)})


def test_anthem_paths_cover_every_combination_and_sum_to_one() -> None:
    grid = expand(anthem())
    paths = list(grid.paths())
    assert len(paths) == 3 * 3  # solo: 0-2 extensions; chorus3: 1-3 visits
    assert sum(p.prob for p in paths) == pytest.approx(1.0)
    best = max(paths, key=lambda p: p.prob)
    assert best.prob == pytest.approx(0.7 * 0.8)
    assert all(
        p.positions[0] == grid.start and p.positions[-1] == pos("outro", 1, 4) for p in paths
    )


def test_visits_are_numbered_per_section() -> None:
    grid = expand(anthem())
    visits = {(p.section, p.visit) for p in grid.positions}
    assert ("verse1", 1) in visits and ("verse2", 1) in visits  # distinct sections, own counters
    assert {v for s, v in visits if s == "chorus3"} == {1, 2, 3}


def test_expansion_is_deterministic() -> None:
    assert expand(anthem()) == expand(anthem())


def test_path_limit_fails_loudly() -> None:
    grid = expand(anthem())
    assert grid.path_count() == 9
    with pytest.raises(ArrangementError, match=r"9 possible paths.*limit of 8"):
        list(grid.paths(max_paths=8))


# --- Graph edges: skips, explicit next, early end ----------------------------------------


def skipping() -> Song:
    return song_from(
        [
            {"id": "verse1", "bars": 2},
            {"id": "verse2", "bars": 2},
            {"id": "bridge", "bars": 2},
            {"id": "chorus", "bars": 2, "ends": True},
        ],
        graph=[{"from": "verse1", "to": "bridge", "prob": 0.25}],
    )


def test_a_forward_edge_takes_its_probability_and_the_rest_goes_to_the_next_section() -> None:
    assert targets(skipping(), pos("verse1", 1, 2)) == {
        pos("bridge", 1, 1): pytest.approx(0.25),
        pos("verse2", 1, 1): pytest.approx(0.75),
    }
    kinds = {t.to: t.kind for t in expand(skipping()).successors(pos("verse1", 1, 2))}
    assert kinds == {pos("bridge", 1, 1): "jump", pos("verse2", 1, 1): "next"}


def test_an_explicit_edge_to_the_next_section_adds_to_the_remainder() -> None:
    song = song_from(
        [{"id": "a", "bars": 1}, {"id": "b", "bars": 1}, {"id": "c", "bars": 1, "ends": True}],
        graph=[
            {"from": "a", "to": "b", "prob": 0.4},
            {"from": "a", "to": "c", "prob": 0.1},
        ],
    )
    assert targets(song, pos("a", 1, 1)) == {
        pos("b", 1, 1): pytest.approx(0.9),
        pos("c", 1, 1): pytest.approx(0.1),
    }


def test_sections_nothing_can_reach_are_left_out() -> None:
    song = song_from(
        [
            {"id": "a", "bars": 1},
            {"id": "b", "bars": 1, "ends": True},
            {"id": "c", "bars": 1, "ends": True},
        ]
    )
    grid = expand(song)
    assert {p.section for p in grid.positions} == {"a", "b"}


def test_the_end_of_the_song_is_a_transition_to_none() -> None:
    grid = expand(skipping())
    assert [(t.to, t.kind) for t in grid.successors(pos("chorus", 1, 2))] == [(None, "end")]


# --- Things v0 cannot expand, with clear messages ----------------------------------------


def test_backward_edges_are_not_supported() -> None:
    song = song_from(
        [{"id": "a", "bars": 1}, {"id": "b", "bars": 1}, {"id": "c", "bars": 1, "ends": True}],
        graph=[{"from": "b", "to": "a", "prob": 0.1}],
    )
    with pytest.raises(
        ArrangementError, match=r"graph edge b -> a goes back to an earlier section"
    ):
        expand(song)


def test_a_self_loop_must_agree_with_the_section_probability() -> None:
    song = song_from(
        [
            {"id": "a", "bars": 1, "extendable": {"unit": 1, "prob": 0.3}},
            {"id": "b", "bars": 1, "ends": True},
        ],
        graph=[{"from": "a", "to": "a", "prob": 0.5}],
    )
    with pytest.raises(ArrangementError, match=r"section 'a': .*0\.5.*0\.3"):
        expand(song)


def test_a_self_loop_on_a_section_that_both_extends_and_repeats_is_ambiguous() -> None:
    song = song_from(
        [
            {
                "id": "a",
                "bars": 1,
                "extendable": {"unit": 1, "prob": 0.3},
                "repeat": {"max": 1, "prob": 0.3},
            },
            {"id": "b", "bars": 1, "ends": True},
        ],
        graph=[{"from": "a", "to": "a", "prob": 0.3}],
    )
    with pytest.raises(ArrangementError, match=r"section 'a': .*both extendable and repeat"):
        expand(song)


def test_a_section_may_extend_and_repeat_when_each_probability_is_its_own() -> None:
    song = song_from(
        [
            {
                "id": "a",
                "bars": 1,
                "extendable": {"unit": 1, "prob": 0.2},
                "repeat": {"max": 1, "prob": 0.3},
            },
            {"id": "b", "bars": 1, "ends": True},
        ]
    )
    assert targets(song, pos("a", 1, 1)) == {
        pos("a", 1, 2): pytest.approx(0.2),
        pos("a", 2, 1): pytest.approx(0.3),
        pos("b", 1, 1): pytest.approx(0.5),
    }


def test_outcomes_that_add_up_to_more_than_one_are_rejected() -> None:
    song = song_from(
        [
            {"id": "a", "bars": 1, "extendable": {"unit": 1, "prob": 0.6}},
            {"id": "b", "bars": 1},
            {"id": "c", "bars": 1, "ends": True},
        ],
        graph=[{"from": "a", "to": "c", "prob": 0.6}],
    )
    with pytest.raises(ArrangementError, match=r"section 'a': .*1\.2.*more than 1"):
        expand(song)


def test_a_section_that_can_never_be_left_is_rejected() -> None:
    song = song_from([{"id": "a", "bars": 1, "extendable": {"unit": 1, "prob": 1.0}, "ends": True}])
    with pytest.raises(ArrangementError, match=r"section 'a': .*no way forward"):
        expand(song)


def test_a_bad_extension_cap_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_extensions"):
        expand(linear(), max_extensions=-1)
