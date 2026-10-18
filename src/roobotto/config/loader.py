"""Load YAML config files into validated models, with clear, located error messages."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from roobotto.config.rig import Rig
from roobotto.config.setlist import Setlist
from roobotto.config.song import Song

Config = Song | Rig | Setlist
KINDS: dict[str, type[Song] | type[Rig] | type[Setlist]] = {
    "song": Song,
    "rig": Rig,
    "setlist": Setlist,
}


@dataclass(frozen=True)
class Issue:
    path: str
    location: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}: {self.location}: " if self.location else f"{self.path}: "
        return f"{where}{self.message}"


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            try:
                duplicate = key in seen
            except TypeError:
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)  # pyright: ignore[reportUnknownArgumentType]
        return super().construct_mapping(node, deep)


def _location(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def issues_from_validation_error(path: str, error: ValidationError) -> list[Issue]:
    issues: list[Issue] = []
    for item in error.errors():
        message = item["msg"].removeprefix("Value error, ")
        for line in message.split("\n"):
            issues.append(Issue(path, _location(item["loc"]), line))
    return issues


def load_config(path: Path) -> tuple[Config | None, list[Issue]]:
    """Parse one YAML file. Returns the model, or ``None`` plus the issues found."""
    name = str(path)
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except OSError as exc:
        return None, [Issue(name, "", f"cannot read file: {exc.strerror or exc}")]
    except yaml.YAMLError as exc:
        return None, [Issue(name, "", f"invalid YAML: {str(exc).replace(chr(10), ' ')}")]
    if not isinstance(raw, dict):
        return None, [Issue(name, "", "the file must contain a YAML mapping")]
    data: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    kind = data.get("kind")
    model = KINDS.get(kind) if isinstance(kind, str) else None
    if model is None:
        return None, [Issue(name, "kind", f"must be one of {', '.join(sorted(KINDS))}")]
    try:
        return model.model_validate(data), []
    except ValidationError as exc:
        return None, issues_from_validation_error(name, exc)


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(by_alias=True, mode="json")
