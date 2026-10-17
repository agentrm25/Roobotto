"""Shared base model and strict scalar types.

Every schema forbids unknown keys and is immutable. Scalars are strict so that YAML
quirks (``on``/``no`` becoming booleans, ``true`` becoming ``1``) are rejected, not coerced.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Model(BaseModel):
    """Base for every schema: no unknown keys, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def raise_if(errors: list[str]) -> None:
    """Raise one ``ValueError`` carrying every message, one per line."""
    if errors:
        raise ValueError("\n".join(errors))


Id = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$"),
]
"""Lowercase identifier: section, song, device, port, cue and command names."""

IntentKey = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}$"),
]
"""``device.intent``, for example ``mvp.scene``. Cues are intents, never bytes."""

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
EvidenceId = Annotated[str, StringConstraints(strict=True, pattern=r"^E-\d{2,}$")]
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]

PosInt = Annotated[int, Field(strict=True, ge=1)]
NonNegInt = Annotated[int, Field(strict=True, ge=0)]
Channel = Annotated[int, Field(strict=True, ge=1, le=16)]
"""MIDI channel 1-16. Converted to 0-15 only in adapters/midi_wire.py (ADR-007, I-10)."""
Midi7 = Annotated[int, Field(strict=True, ge=0, le=127)]
Ns = Annotated[int, Field(strict=True, ge=0)]
"""Nanoseconds on the injected clock (ADR-005)."""
Ms = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Prob = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
PosFloat = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]
