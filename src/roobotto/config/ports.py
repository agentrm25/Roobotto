"""Ports file v0 (T-06): rig port ids -> the operating system's port names.

System port names are machine-specific, so they never go in the rig (ADR-008). The real file is
``local/ports.yaml`` (ignored by git); ``examples/ports.template.yaml`` is the skeleton.
``roobotto devices ports-template <rig>`` writes one with the names seen on this machine as hints.
"""

from typing import Any, Literal

from pydantic import Field, model_validator

from roobotto.config.common import Id, Model, Text


class Ports(Model):
    kind: Literal["ports"]
    schema_version: Literal[0]
    id: Id
    rig: Id
    """The rig this file names ports for. Checked against the rig's id."""
    ports: dict[Id, Text | None] = Field(default_factory=lambda: {})
    """Rig port id -> system MIDI port name, or ``null`` while not yet chosen."""
    audio_input: Text | None = None
    """System audio input device for capture tools, by name. Optional."""

    @model_validator(mode="before")
    @classmethod
    def _reject_arming(cls, data: Any) -> Any:
        if isinstance(data, dict):
            keys = {str(k).lower() for k in data}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if keys & {"armed", "arm", "arming", "enabled", "override"}:
                raise ValueError(
                    "arming and overrides are never configured: outputs are armed explicitly "
                    "for each run and never persist (ADR-009, I-1, I-2)"
                )
        return data  # pyright: ignore[reportUnknownVariableType]
