"""The normalized decision hash (ADR-014, I-19).

Records are serialized canonically (sorted keys, no whitespace), one per line, with absolute
times made relative to the start of the trace. Every key ending in ``_ns`` is an absolute time,
except durations, which end in ``_dur_ns``. The hash excludes run ids, the config hash (it is
part of the claim, not of each decision) and physical delivery results.
"""

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from roobotto.engine.show import canonical_json

EXCLUDED_KEYS = frozenset({"run_id", "config_hash", "result"})


def _is_absolute_time(key: str) -> bool:
    return key.endswith("_ns") and not key.endswith("_dur_ns")


def normalize(value: Any, origin_ns: int) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():  # pyright: ignore[reportUnknownVariableType]
            key = str(k)  # pyright: ignore[reportUnknownArgumentType]
            if key in EXCLUDED_KEYS:
                continue
            if _is_absolute_time(key) and isinstance(v, int) and not isinstance(v, bool):
                out[key] = v - origin_ns
            else:
                out[key] = normalize(v, origin_ns)
        return out
    if isinstance(value, list | tuple):
        return [normalize(v, origin_ns) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def decision_hash(records: Iterable[Mapping[str, Any]], origin_ns: int) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(canonical_json(normalize(record, origin_ns)).encode())
        digest.update(b"\n")
    return digest.hexdigest()
