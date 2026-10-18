"""Setlist schema v0: the ordered songs and how a Helix preset change selects one (H-2)."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from roobotto.config.common import Id, Midi7, Model, Text, raise_if


class SetlistEntry(Model):
    song: Id
    program_change: Midi7 | None = None
    """Helix Program Change that selects this song. ``null`` means the HUD's "next song"."""


class Setlist(Model):
    kind: Literal["setlist"]
    schema_version: Literal[0]
    id: Id
    title: Text
    entries: Annotated[list[SetlistEntry], Field(min_length=1)]

    @model_validator(mode="after")
    def _check_program_changes(self) -> Self:
        numbers = [e.program_change for e in self.entries if e.program_change is not None]
        errors = [
            f"program_change {n} selects more than one setlist entry"
            for n in sorted({n for n in numbers if numbers.count(n) > 1})
        ]
        raise_if(errors)
        return self
