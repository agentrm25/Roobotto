"""T-03 property tests: the grid is a well-formed probability graph; the classes partition it."""

from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from roobotto.arrangement import BarGrid, expand
from roobotto.config.song import Song
from roobotto.cues.equivalence import EquivalenceClasses, production_classes

MAX_PATHS = 2000
PROPERTY_SETTINGS = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

METERS = {"4/4": 4, "3/4": 3, "6/8": 6}
INTENTS = ["mvp.scene", "aum.layer"]
VALUES = ["a", "b", "c"]
OFFSETS = [0.0, -1.0, -2.0, -0.5, 0.5, 1.0]


@st.composite
def cue_doc(
    draw: st.DrawFn, sections: list[dict[str, Any]], meter: str, cue_id: str
) -> dict[str, Any]:
    section = draw(st.sampled_from(sections))
    return {
        "id": cue_id,
        "at": {
            "section": section["id"],
            "bar": draw(st.integers(1, section["bars"])),
            "beat": draw(st.integers(1, METERS[meter])),
            "offset_beats": draw(st.sampled_from(OFFSETS)),
        },
        "class": draw(st.sampled_from(["decorative", "standard"])),
        "state": {draw(st.sampled_from(INTENTS)): draw(st.sampled_from(VALUES))},
    }


@st.composite
def song_docs(draw: st.DrawFn) -> dict[str, Any]:
    """A random, mostly valid song: loops, forward skips, early ends and a small cue sheet."""
    meter = draw(st.sampled_from(sorted(METERS)))
    count = draw(st.integers(min_value=2, max_value=5))
    sections: list[dict[str, Any]] = []
    graph: list[dict[str, Any]] = []
    for index in range(count):
        section: dict[str, Any] = {"id": f"s{index}", "bars": draw(st.integers(1, 3))}
        last = index == count - 1
        ends = last or draw(st.integers(0, 9)) == 0
        if ends:
            section["ends"] = True
        loop = draw(st.sampled_from(["none", "none", "extend", "repeat"]))
        prob = draw(st.sampled_from([0.2, 0.3, 0.5]))
        if loop == "extend":
            section["extendable"] = {"unit": draw(st.integers(1, 2)), "prob": prob}
        elif loop == "repeat":
            section["repeat"] = {"max": draw(st.integers(1, 2)), "prob": prob}
        if loop != "none" and not ends and draw(st.booleans()):
            graph.append({"from": section["id"], "to": section["id"], "prob": prob})
        if not ends and index + 2 < count:  # skips: never to the next section, that is implicit
            skips = draw(st.sets(st.integers(index + 2, count - 1), max_size=2))
            graph.extend({"from": section["id"], "to": f"s{t}", "prob": 0.2} for t in sorted(skips))
        sections.append(section)
    cues = [draw(cue_doc(sections, meter, f"c{n}")) for n in range(draw(st.integers(0, 4)))]
    return {
        "kind": "song",
        "schema_version": 0,
        "id": "random_song",
        "title": "Random song",
        "tempo": {"nominal": 120, "range": [100, 140]},
        "meter": meter,
        "sections": sections,
        "graph": graph,
        "cues": cues,
    }


def build(doc: dict[str, Any]) -> Song:
    try:
        return Song.model_validate(doc)
    except ValidationError:
        assume(False)
        raise  # unreachable; assume(False) always rejects the example


def grid_for(doc: dict[str, Any]) -> BarGrid:
    grid = expand(build(doc))
    assume(grid.path_count() <= MAX_PATHS)
    return grid


@PROPERTY_SETTINGS
@given(song_docs())
def test_grid_is_a_probability_graph_that_always_ends(doc: dict[str, Any]) -> None:
    grid = grid_for(doc)
    order = {p: i for i, p in enumerate(grid.positions)}
    assert grid.start == grid.positions[0]
    for position in grid.positions:
        transitions = grid.successors(position)
        assert transitions
        assert sum(t.prob for t in transitions) == pytest.approx(1.0)
        assert all(t.prob > 0 for t in transitions)
        for t in transitions:
            if t.to is not None:
                assert t.to in order and order[t.to] > order[position]  # a DAG in play order
    assert grid.path_count() >= 1  # every cell can reach the end of the song


@PROPERTY_SETTINGS
@given(song_docs())
def test_path_probabilities_sum_to_one(doc: dict[str, Any]) -> None:
    grid = grid_for(doc)
    paths = list(grid.paths(max_paths=MAX_PATHS))
    assert len(paths) == grid.path_count()
    assert sum(p.prob for p in paths) == pytest.approx(1.0)


def assert_partition(grid: BarGrid, classes: EquivalenceClasses) -> None:
    everything = list(grid.beat_positions())
    members = [m for cls in classes.classes for m in cls]
    assert all(cls for cls in classes.classes), "no empty class"
    assert len(members) == len(set(members)), "no position in two classes"
    assert set(members) == set(everything), "every position in some class"
    for index, cls in enumerate(classes.classes):
        assert all(classes.class_of(m) == index for m in cls)


@PROPERTY_SETTINGS
@given(song_docs(), st.integers(min_value=1, max_value=12))
def test_classes_partition_the_positions(doc: dict[str, Any], horizon: int) -> None:
    grid = grid_for(doc)
    assert_partition(grid, production_classes(grid, horizon_beats=horizon, max_paths=MAX_PATHS))


@PROPERTY_SETTINGS
@given(song_docs(), st.integers(min_value=1, max_value=10))
def test_a_longer_horizon_only_splits_classes(doc: dict[str, Any], horizon: int) -> None:
    grid = grid_for(doc)
    short = production_classes(grid, horizon_beats=horizon, max_paths=MAX_PATHS)
    long = production_classes(grid, horizon_beats=horizon + 1, max_paths=MAX_PATHS)
    for cls in long.classes:
        assert len({short.class_of(m) for m in cls}) == 1


@PROPERTY_SETTINGS
@given(song_docs(), st.integers(min_value=1, max_value=12))
def test_renaming_cues_changes_nothing(doc: dict[str, Any], horizon: int) -> None:
    grid = grid_for(doc)
    renamed = {**doc, "cues": [{**c, "id": f"renamed_{c['id']}"} for c in doc["cues"]]}
    other = production_classes(expand(build(renamed)), horizon_beats=horizon, max_paths=MAX_PATHS)
    assert (
        other.classes
        == production_classes(grid, horizon_beats=horizon, max_paths=MAX_PATHS).classes
    )


@PROPERTY_SETTINGS
@given(song_docs(), st.integers(min_value=1, max_value=12), st.data())
def test_a_cue_on_a_new_device_can_only_split_classes(
    doc: dict[str, Any], horizon: int, data: st.DataObject
) -> None:
    grid = grid_for(doc)
    extra = data.draw(cue_doc(doc["sections"], doc["meter"], "extra"))
    extra["state"] = {"fresh.intent": "on"}
    with_extra = production_classes(
        expand(build({**doc, "cues": [*doc["cues"], extra]})),
        horizon_beats=horizon,
        max_paths=MAX_PATHS,
    )
    before = production_classes(grid, horizon_beats=horizon, max_paths=MAX_PATHS)
    for cls in with_extra.classes:
        assert len({before.class_of(m) for m in cls}) == 1
