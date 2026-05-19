"""Typed distribution profile parsing and Nanvix command-line encoding."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProfileError(ValueError):
    """Raised when a distribution profile is invalid."""


def validate_profile_name(name: str) -> str:
    """Validate and return a safe distribution/profile name."""
    if not _NAME_PATTERN.fullmatch(name):
        raise ProfileError("profile name contains unsupported characters")
    return name


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProfileError(f"{field} must be a table")

    result: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise ProfileError(f"{field} contains a non-string key")
        result[key] = item
    return result


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ProfileError(f"{field} must be an array")
    return list(cast(list[object], value))


def _required_string(data: dict[str, object], key: str, field: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{field}.{key} must be a non-empty string")
    return value


def _optional_string(
    data: dict[str, object], key: str, field: str, *, default: str = ""
) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ProfileError(f"{field}.{key} must be a string")
    return value


def _reject_unknown(data: dict[str, object], allowed: set[str], field: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ProfileError(f"{field} contains unknown key(s): {', '.join(unknown)}")


def _relative_path(value: str, field: str) -> PurePosixPath:
    if "\\" in value or "\0" in value:
        raise ProfileError(f"{field} must use a safe POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise ProfileError(f"{field} must be a non-empty relative path")
    return path


def _destination_path(value: str, field: str) -> PurePosixPath:
    if "\\" in value or "\0" in value:
        raise ProfileError(f"{field} must use a safe POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ProfileError(f"{field} must be an absolute guest path without '..'")
    return path


def _validate_source(value: str, field: str) -> str:
    if value in {"runtime", "profile"}:
        return value
    if value.startswith("package:") and _NAME_PATTERN.fullmatch(
        value.removeprefix("package:")
    ):
        return value
    raise ProfileError(f"{field} must be runtime, profile, or package:<name>")


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{field} must be a non-empty string")
    if any(character.isspace() for character in value):
        raise ProfileError(f"{field} cannot contain whitespace")
    if "\0" in value:
        raise ProfileError(f"{field} cannot contain NUL")
    return value


@dataclass(frozen=True)
class ArtifactRef:
    """A path rooted in the runtime, a package export, or the profile directory."""

    source: str
    path: PurePosixPath


@dataclass(frozen=True)
class Program:
    """One ordered ELF entry in a Nanvix multibinary image."""

    artifact: ArtifactRef
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]

    @property
    def command_line(self) -> str:
        """Encode argv and environment for Nanvix's boot command-line format."""
        return encode_command_line(self.argv, self.env)


@dataclass(frozen=True)
class RamfsEntry:
    """One file or directory layer copied into the guest RAM filesystem."""

    artifact: ArtifactRef
    destination: PurePosixPath
    normalize_newlines: bool = False


@dataclass(frozen=True)
class ImageProfile:
    """A complete declarative Nanvix distribution image."""

    name: str
    kernel_args: str
    programs: tuple[Program, ...]
    ramfs_directories: tuple[PurePosixPath, ...]
    ramfs_entries: tuple[RamfsEntry, ...]
    ramfs_image: ArtifactRef | None


def _artifact(data: dict[str, object], field: str) -> ArtifactRef:
    source = _validate_source(
        _required_string(data, "source", field), f"{field}.source"
    )
    path = _relative_path(_required_string(data, "path", field), f"{field}.path")
    return ArtifactRef(source=source, path=path)


def _arguments(value: object, field: str) -> tuple[str, ...]:
    values = _list(value, field)
    return tuple(_token(item, f"{field}[{index}]") for index, item in enumerate(values))


def _environment(value: object, field: str) -> tuple[tuple[str, str], ...]:
    data = _mapping(value, field)
    env: list[tuple[str, str]] = []
    for key in sorted(data):
        if not _ENV_NAME_PATTERN.fullmatch(key):
            raise ProfileError(f"{field} contains invalid variable name {key!r}")
        env.append((key, _token(data[key], f"{field}.{key}")))
    return tuple(env)


def _program(value: object, index: int) -> Program:
    field = f"program[{index}]"
    data = _mapping(value, field)
    _reject_unknown(data, {"source", "path", "argv", "env"}, field)
    artifact = _artifact(data, field)

    argv = _arguments(data.get("argv"), f"{field}.argv")
    if not argv:
        raise ProfileError(f"{field}.argv must contain argv[0]")
    return Program(
        artifact=artifact,
        argv=argv,
        env=_environment(data.get("env", {}), f"{field}.env"),
    )


def _ramfs_entry(value: object, index: int) -> RamfsEntry:
    field = f"ramfs[{index}]"
    data = _mapping(value, field)
    _reject_unknown(data, {"source", "path", "destination"}, field)
    return RamfsEntry(
        artifact=_artifact(data, field),
        destination=_destination_path(
            _required_string(data, "destination", field), f"{field}.destination"
        ),
    )


def _ramfs_image(value: object) -> ArtifactRef:
    field = "ramfs-image"
    data = _mapping(value, field)
    _reject_unknown(data, {"source", "path"}, field)
    return _artifact(data, field)


def _guest_init(value: object) -> tuple[Program, RamfsEntry]:
    field = "init"
    data = _mapping(value, field)
    _reject_unknown(
        data,
        {
            "source",
            "path",
            "interpreter",
            "script",
            "destination",
            "args",
            "env",
        },
        field,
    )
    destination = _destination_path(
        _optional_string(data, "destination", field, default="/init"),
        f"{field}.destination",
    )
    interpreter = _token(
        _required_string(data, "interpreter", field),
        f"{field}.interpreter",
    )
    extra_args = _arguments(data.get("args", []), f"{field}.args")
    program = Program(
        artifact=_artifact(data, field),
        argv=(
            interpreter,
            _token(str(destination), f"{field}.destination"),
            *extra_args,
        ),
        env=_environment(data.get("env", {}), f"{field}.env"),
    )
    script = RamfsEntry(
        artifact=ArtifactRef(
            source="profile",
            path=_relative_path(
                _required_string(data, "script", field),
                f"{field}.script",
            ),
        ),
        destination=destination,
        normalize_newlines=True,
    )
    return program, script


def load_profile(path: Path) -> ImageProfile:
    """Load and strictly validate a TOML distribution profile."""
    try:
        parsed = cast(object, tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from exc

    root = _mapping(parsed, str(path))
    _reject_unknown(
        root,
        {
            "name",
            "kernel-args",
            "program",
            "init",
            "ramfs-directories",
            "ramfs",
            "ramfs-image",
        },
        "profile",
    )

    name = _required_string(root, "name", "profile")
    validate_profile_name(name)

    kernel_args = _optional_string(root, "kernel-args", "profile")
    if any(character in kernel_args for character in ("\0", "\n", "\r")):
        raise ProfileError("profile.kernel-args must be a single line")

    program_values = _list(root.get("program", []), "profile.program")
    programs = [_program(value, index) for index, value in enumerate(program_values)]

    ramfs_values = _list(root.get("ramfs", []), "profile.ramfs")
    ramfs_entries = [
        _ramfs_entry(value, index) for index, value in enumerate(ramfs_values)
    ]
    if "init" in root:
        init_program, init_script = _guest_init(root["init"])
        programs.append(init_program)
        ramfs_entries.append(init_script)
    if not programs:
        raise ProfileError("profile must contain a program or init entry")

    directory_values = _list(
        root.get("ramfs-directories", []),
        "profile.ramfs-directories",
    )
    ramfs_directories = tuple(
        _destination_path(
            _token(value, f"profile.ramfs-directories[{index}]"),
            f"profile.ramfs-directories[{index}]",
        )
        for index, value in enumerate(directory_values)
    )
    if len(set(ramfs_directories)) != len(ramfs_directories):
        raise ProfileError("profile.ramfs-directories contains a duplicate path")

    ramfs_image = _ramfs_image(root["ramfs-image"]) if "ramfs-image" in root else None
    if (ramfs_entries or ramfs_directories) and ramfs_image is not None:
        raise ProfileError(
            "profile cannot combine RAMFS staging entries with ramfs-image"
        )

    return ImageProfile(
        name=name,
        kernel_args=kernel_args,
        programs=tuple(programs),
        ramfs_directories=ramfs_directories,
        ramfs_entries=tuple(ramfs_entries),
        ramfs_image=ramfs_image,
    )


def encode_command_line(
    argv: tuple[str, ...], env: tuple[tuple[str, str], ...] = ()
) -> str:
    """Encode argv and environment using Nanvix's `<args>;<env>` format."""
    if not argv:
        raise ProfileError("argv must contain argv[0]")

    encoded_argv = " ".join(_escape_semicolons(_token(arg, "argv")) for arg in argv)
    if not env:
        return encoded_argv

    encoded_env: list[str] = []
    for key, value in env:
        if not _ENV_NAME_PATTERN.fullmatch(key):
            raise ProfileError(f"invalid environment variable name {key!r}")
        encoded_env.append(f"{key}={_escape_semicolons(_token(value, f'env.{key}'))}")
    return f"{encoded_argv};{' '.join(encoded_env)}"


def _escape_semicolons(value: str) -> str:
    return value.replace(";", r"\;")
