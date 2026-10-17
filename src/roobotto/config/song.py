"""Song / Arrangement schema v0: sections, bar graph, and the cue sheet."""

import re
from collections import defaultdict
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from roobotto.config.common import (
    Finite,
    Id,
    IntentKey,
    Model,
    PosFloat,
    PosInt,
    Prob,
    Text,
    raise_if,
)

PERFORMER_INTENT_CORROBORATION = frozenset({"footswitch", "mapped_snapshot"})
"""Corroboration sources that are performer intent (ADR-011). The others are tracker evidence."""

_PROB_EPSILON = 1e-9
_METER = re.compile(r"^([1-9][0-9]?)/(2|4|8|16)$")


class Tempo(Model):
    nominal: PosFloat
    range: Annotated[list[PosFloat], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        low, high = self.range
        raise_if(
            [
                msg
                for ok, msg in [
                    (low <= high, f"tempo range [{low}, {high}] must be [low, high]"),
                    (
                        low <= self.nominal <= high,
                        f"nominal tempo {self.nominal} is outside range [{low}, {high}]",
                    ),
                ]
                if not ok
            ]
        )
        return self


class Extendable(Model):
    """The section may run longer, in blocks of ``unit`` bars."""

    unit: PosInt
    prob: Prob


class Repeat(Model):
    """The section may repeat up to ``max`` extra times."""

    max: PosInt
    prob: Prob


class AdvanceOn(Model):
    """The Helix snapshot change (1-8) that advances the band *into* this section.

    Only mapped transitions count as performer intent (ADR-011, Section 5.4).
    """

    snapshot: Annotated[int, Field(strict=True, ge=1, le=8)]


class Section(Model):
    id: Id
    bars: PosInt
    vocal: bool = Field(default=False, strict=True)
    lyric_anchor: Text | None = None
    model_as: Id | None = None
    extendable: Extendable | None = None
    repeat: Repeat | None = None
    ends: bool = Field(default=False, strict=True)
    advance_on: AdvanceOn | None = None

    @model_validator(mode="after")
    def _check_lyric_anchor(self) -> Self:
        if self.lyric_anchor is not None and not self.vocal:
            raise ValueError(f"section '{self.id}': lyric_anchor requires vocal: true")
        return self


class Edge(Model):
    """A non-sequential transition. The remainder of probability goes to the next section."""

    from_: Id = Field(alias="from")
    to: Id
    prob: Annotated[float, Field(strict=True, gt=0, le=1, allow_inf_nan=False)]


class CueAt(Model):
    section: Id
    bar: PosInt = 1
    beat: PosInt = 1
    offset_beats: Finite = 0.0
    """Shifts the cue relative to bar/beat. Negative values anticipate."""


class FireLateWithin(Model):
    fire_late_within_beats: PosFloat


OnMiss = Literal["skip", "send_now"] | FireLateWithin

Corroboration = Literal["footswitch", "mapped_snapshot", "drum_pattern", "vocal_activity"]


class Requires(Model):
    posterior_min: Prob | None = None
    corroborate_by: list[Corroboration] = Field(default_factory=lambda: [])
    within_bars: PosInt | None = None


StateValue = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z0-9][a-z0-9_]{0,63}$")]


class Cue(Model):
    id: Id
    at: CueAt
    cue_class: Literal["decorative", "standard", "critical"] = Field(alias="class")
    state: Annotated[dict[IntentKey, StateValue], Field(min_length=1)]
    on_miss: OnMiss = "send_now"
    requires: Requires | None = None
    on_uncertain: Literal["hold", "ask", "abstain"] = "hold"

    @field_validator("cue_class", mode="before")
    @classmethod
    def _reject_excluded(cls, value: object) -> object:
        if value == "excluded":
            raise ValueError(
                "class 'excluded' is never automated; remove the cue from the cue sheet "
                "and use an independent interlock (Section 5.1)"
            )
        return value

    @model_validator(mode="after")
    def _critical_needs_performer_intent(self) -> Self:
        if self.cue_class != "critical":
            return self
        sources: set[str] = set(self.requires.corroborate_by) if self.requires else set()
        if not sources & PERFORMER_INTENT_CORROBORATION:
            raise ValueError(
                f"cue '{self.id}': a critical cue must require a performer-intent source "
                "(footswitch or mapped_snapshot) in requires.corroborate_by; tracker "
                "evidence alone never fires a critical cue (ADR-013, I-14)"
            )
        return self


class Song(Model):
    kind: Literal["song"]
    schema_version: Literal[0]
    id: Id
    title: Text
    tempo: Tempo
    meter: Annotated[str, StringConstraints(strict=True)]
    sections: Annotated[list[Section], Field(min_length=1)]
    graph: list[Edge] = Field(default_factory=lambda: [])
    cues: list[Cue] = Field(default_factory=lambda: [])

    @field_validator("meter")
    @classmethod
    def _check_meter(cls, value: str) -> str:
        if not _METER.match(value):
            raise ValueError(f"meter '{value}' must look like 4/4, 6/8 or 7/8")
        return value

    @property
    def beats_per_bar(self) -> int:
        match = _METER.match(self.meter)
        assert match is not None
        return int(match.group(1))

    def section(self, section_id: str) -> Section | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def successors(self) -> dict[str, list[str]]:
        """Possible next sections: the following section (unless this one ends) and edges."""
        result: dict[str, list[str]] = {s.id: [] for s in self.sections}
        for index, sec in enumerate(self.sections):
            if not sec.ends and index + 1 < len(self.sections):
                result[sec.id].append(self.sections[index + 1].id)
        for edge in self.graph:
            if edge.from_ in result and edge.to not in result[edge.from_]:
                result[edge.from_].append(edge.to)
        return result

    @model_validator(mode="after")
    def _check_sections(self) -> Self:
        errors: list[str] = []
        ids = [s.id for s in self.sections]
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            errors.append(f"duplicate section id '{dup}'")
        known = set(ids)
        by_id = {s.id: s for s in self.sections}
        for sec in self.sections:
            if sec.model_as is None:
                continue
            if sec.model_as == sec.id:
                errors.append(f"section '{sec.id}': model_as cannot refer to itself")
            elif sec.model_as not in known:
                errors.append(
                    f"section '{sec.id}': model_as refers to unknown section '{sec.model_as}'"
                )
        for sec in self.sections:
            seen = {sec.id}
            current = by_id.get(sec.model_as) if sec.model_as else None
            while current is not None:
                if current.id in seen:
                    errors.append(f"section '{sec.id}': model_as chain contains a cycle")
                    break
                seen.add(current.id)
                current = by_id.get(current.model_as) if current.model_as else None
        if not self.sections[-1].ends:
            errors.append(
                f"the last section '{self.sections[-1].id}' must set ends: true "
                "so the song has a defined end"
            )
        raise_if(errors)
        return self

    @model_validator(mode="after")
    def _check_graph(self) -> Self:
        errors: list[str] = []
        by_id = {s.id: s for s in self.sections}
        seen: set[tuple[str, str]] = set()
        totals: dict[str, float] = defaultdict(float)
        for edge in self.graph:
            label = f"graph edge {edge.from_} -> {edge.to}"
            for end in (edge.from_, edge.to):
                if end not in by_id:
                    errors.append(f"{label}: unknown section '{end}'")
            if (edge.from_, edge.to) in seen:
                errors.append(f"{label}: duplicate edge")
            seen.add((edge.from_, edge.to))
            source = by_id.get(edge.from_)
            if source is not None:
                if source.ends:
                    errors.append(
                        f"{label}: section '{source.id}' ends the song and has no successors"
                    )
                if edge.from_ == edge.to and source.extendable is None and source.repeat is None:
                    errors.append(
                        f"{label}: a self-loop needs the section to be extendable or repeat"
                    )
            totals[edge.from_] += edge.prob
        for source_id, total in totals.items():
            if total > 1 + _PROB_EPSILON:
                errors.append(
                    f"graph edges from '{source_id}' have probabilities "
                    f"summing to {total:.3g} (> 1)"
                )
        raise_if(errors)
        return self

    @model_validator(mode="after")
    def _check_advance_on(self) -> Self:
        errors: list[str] = []
        by_id = {s.id: s for s in self.sections}
        if self.sections[0].advance_on is not None:
            errors.append(
                f"section '{self.sections[0].id}': the first section is entered by song "
                "selection, so it cannot have advance_on"
            )
        for source, targets in self.successors().items():
            snapshots: dict[int, str] = {}
            for target in targets:
                advance = by_id[target].advance_on if target in by_id else None
                if advance is None:
                    continue
                other = snapshots.get(advance.snapshot)
                if other is not None:
                    errors.append(
                        f"advance_on snapshot {advance.snapshot} is ambiguous after '{source}': "
                        f"it maps to both '{other}' and '{target}'"
                    )
                snapshots[advance.snapshot] = target
        raise_if(errors)
        return self

    @model_validator(mode="after")
    def _check_cues(self) -> Self:
        errors: list[str] = []
        by_id = {s.id: s for s in self.sections}
        ids = [c.id for c in self.cues]
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            errors.append(f"duplicate cue id '{dup}' (cue identity is the occurrence, ADR-012)")
        bpb = self.beats_per_bar
        for cue in self.cues:
            where = f"cue '{cue.id}'"
            sec = by_id.get(cue.at.section)
            if sec is None:
                errors.append(f"{where}: unknown section '{cue.at.section}'")
                continue
            if cue.at.bar > sec.bars:
                errors.append(
                    f"{where}: bar {cue.at.bar} is past the end of section "
                    f"'{sec.id}' ({sec.bars} bars)"
                )
                continue
            if cue.at.beat > bpb:
                errors.append(f"{where}: beat {cue.at.beat} is outside the {self.meter} bar")
                continue
            position = (cue.at.bar - 1) * bpb + (cue.at.beat - 1) + cue.at.offset_beats
            if position >= sec.bars * bpb:
                errors.append(
                    f"{where}: offset_beats moves the cue past the end of section '{sec.id}'"
                )
        raise_if(errors)
        return self
