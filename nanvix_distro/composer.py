"""Menu-driven composition of Nanvix distributions."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from nanvix_distro.profile import ProfileError, load_profile, validate_profile_name


class ComposerError(ValueError):
    """Raised when a distribution composition request is invalid."""


@dataclass(frozen=True)
class Component:
    """One selectable distribution component."""

    key: str
    title: str
    description: str


@dataclass(frozen=True)
class GeneratedDistribution:
    """Paths and selections produced by the distribution composer."""

    name: str
    directory: Path
    profile_path: Path
    init_path: Path | None
    components: frozenset[str]


COMPONENTS: tuple[Component, ...] = (
    Component(
        key="busybox",
        title="BusyBox",
        description="ash shell and core utilities (/bin/busybox)",
    ),
    Component(
        key="python",
        title="CPython",
        description="Python 3 interpreter and standard library",
    ),
    Component(
        key="javascript",
        title="QuickJS",
        description="JavaScript interpreter",
    ),
)

_COMPONENT_KEYS = frozenset(component.key for component in COMPONENTS)
_BINARY_COMPONENT_KEYS = _COMPONENT_KEYS
_BOOT_PRIORITY = ("busybox", "python", "javascript")
_ALIASES = {"cpython": "python", "quickjs": "javascript"}


def normalize_components(values: Iterable[str]) -> frozenset[str]:
    """Normalize CLI component names and require at least one binary."""
    selected: set[str] = set()
    for value in values:
        for raw_component in value.split(","):
            component = raw_component.strip().lower()
            if not component:
                raise ComposerError("component names must not be empty")
            component = _ALIASES.get(component, component)
            if component == "all":
                selected.update(_COMPONENT_KEYS)
            elif component in _COMPONENT_KEYS:
                selected.add(component)
            else:
                choices = ", ".join(sorted((*_COMPONENT_KEYS, "all", *_ALIASES)))
                raise ComposerError(
                    f"unknown component {raw_component!r}; choose from: {choices}"
                )
    if not selected & _BINARY_COMPONENT_KEYS:
        raise ComposerError(
            "at least one binary must be selected: busybox, cpython, or quickjs"
        )
    return frozenset(selected)


def components_from_profile(profile_path: Path) -> frozenset[str]:
    """Infer composer selections from an existing generated profile."""
    if not profile_path.is_file():
        return frozenset({"busybox"})

    try:
        profile = load_profile(profile_path)
    except ProfileError as exc:
        raise ComposerError(f"cannot load existing composition: {exc}") from exc

    sources = {program.artifact.source for program in profile.programs} | {
        entry.artifact.source for entry in profile.ramfs_entries
    }
    if profile.ramfs_image is not None:
        sources.add(profile.ramfs_image.source)

    selected: set[str] = set()
    if "package:busybox" in sources:
        selected.add("busybox")
    if "package:cpython" in sources:
        selected.add("python")
    if "package:quickjs" in sources:
        selected.add("javascript")
    return normalize_components(selected)


def select_components(
    name: str,
    initial: frozenset[str],
) -> frozenset[str] | None:
    """Open a menuconfig-style terminal UI and return the selected components."""
    validate_profile_name(name)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ComposerError(
            "menuconfig requires an interactive terminal; use --include for automation"
        )

    try:
        import curses
    except ImportError as exc:
        raise ComposerError(
            "this Python installation has no curses support; use --include"
        ) from exc

    def menu(screen: curses.window) -> frozenset[str] | None:
        selected = set(normalize_components(initial))
        current = 0
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)

        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
                highlight = curses.color_pair(1)
            except curses.error:
                highlight = curses.A_REVERSE
        else:
            highlight = curses.A_REVERSE

        def put(row: int, text: str, attributes: int = 0) -> None:
            height, width = screen.getmaxyx()
            if row >= height or width <= 1:
                return
            try:
                screen.addnstr(row, 0, text, width - 1, attributes)
            except curses.error:
                pass

        while True:
            screen.erase()
            put(0, "Nanvix Distribution Configuration", curses.A_BOLD)
            put(1, f"Distribution: {name}")
            put(3, "Use arrows or j/k to navigate, Space to toggle.")
            put(4, "Enter saves and builds; q cancels.")

            for index, component in enumerate(COMPONENTS):
                enabled = component.key in selected
                marker = "X" if enabled else " "
                line = f"  [{marker}] {component.title} - {component.description}"
                attributes = highlight if index == current else 0
                put(6 + index, line, attributes)

            selected_titles = (
                component.title for component in COMPONENTS if component.key in selected
            )
            summary = ", ".join(selected_titles)
            if not summary:
                summary = "none (select at least one binary)"
            put(
                8 + len(COMPONENTS),
                f"Selected: {summary}",
            )
            screen.refresh()

            key = screen.getch()
            if key in (curses.KEY_UP, ord("k")):
                current = (current - 1) % len(COMPONENTS)
            elif key in (curses.KEY_DOWN, ord("j")):
                current = (current + 1) % len(COMPONENTS)
            elif key == ord(" "):
                component = COMPONENTS[current]
                if component.key in selected:
                    selected.remove(component.key)
                else:
                    selected.add(component.key)
            elif key in (curses.KEY_ENTER, 10, 13):
                if selected & _BINARY_COMPONENT_KEYS:
                    return normalize_components(selected)
                curses.beep()
            elif key in (ord("q"), 27):
                return None

    result = curses.wrapper(menu)
    if result is None:
        return None
    return frozenset(result)


def component_titles(components: Iterable[str]) -> tuple[str, ...]:
    """Return selected component titles in menu order."""
    selected = normalize_components(components)
    return tuple(
        component.title for component in COMPONENTS if component.key in selected
    )


def render_profile(name: str, components: Iterable[str]) -> str:
    """Render a deterministic TOML image profile for the selected components."""
    validate_profile_name(name)
    selected = normalize_components(components)
    boot_component = next(
        component for component in _BOOT_PRIORITY if component in selected
    )

    lines = [
        "# Generated by `python3 z.py menuconfig`.",
        f'name = "{name}"',
        'ramfs-directories = ["/tmp"]',
        "",
        "[[program]]",
        'source = "runtime"',
        'path = "bin/procd.elf"',
        'argv = ["procd"]',
        "",
        "[[program]]",
        'source = "runtime"',
        'path = "bin/memd.elf"',
        'argv = ["memd"]',
        "",
        "[[program]]",
        'source = "runtime"',
        'path = "bin/vfsd.elf"',
        'argv = ["vfsd"]',
    ]

    if boot_component == "busybox":
        lines.extend(
            [
                "",
                "[init]",
                'source = "package:busybox"',
                'path = "bin/busybox.elf"',
                'interpreter = "ash"',
                'script = "rootfs/init"',
                'destination = "/init"',
                "",
                "[init.env]",
                'HOME = "/"',
                'PATH = "/bin:/usr/bin"',
                'TERM = "vt100"',
            ]
        )
        if "python" in selected:
            lines.extend(
                [
                    'PYTHONHOME = "/"',
                    'PYTHONDONTWRITEBYTECODE = "1"',
                    '_PYTHON_SYSCONFIGDATA_NAME = "_sysconfigdata__nanvix_"',
                ]
            )
    elif boot_component == "python":
        lines.extend(
            [
                "",
                "[[program]]",
                'source = "package:cpython"',
                'path = "bin/python.elf"',
                'argv = ["python"]',
                "env = { "
                'PYTHONHOME = "/", '
                'PYTHONDONTWRITEBYTECODE = "1", '
                '_PYTHON_SYSCONFIGDATA_NAME = "_sysconfigdata__nanvix_" }',
            ]
        )
    else:
        lines.extend(
            [
                "",
                "[[program]]",
                'source = "package:quickjs"',
                'path = "bin/qjs.elf"',
                'argv = ["qjs"]',
            ]
        )

    if boot_component == "busybox":
        lines.extend(
            [
                "",
                "[[ramfs]]",
                'source = "package:busybox"',
                'path = "bin/busybox.elf"',
                'destination = "/bin/busybox"',
            ]
        )
    if "python" in selected:
        if boot_component != "python":
            lines.extend(
                [
                    "",
                    "[[ramfs]]",
                    'source = "package:cpython"',
                    'path = "bin/python.elf"',
                    'destination = "/bin/python3"',
                ]
            )
        lines.extend(
            [
                "",
                "[[ramfs]]",
                'source = "package:cpython"',
                'path = "lib"',
                'destination = "/lib"',
            ]
        )
    if "javascript" in selected and boot_component != "javascript":
        lines.extend(
            [
                "",
                "[[ramfs]]",
                'source = "package:quickjs"',
                'path = "bin/qjs.elf"',
                'destination = "/bin/qjs"',
            ]
        )
    return "\n".join(lines) + "\n"


def render_init(name: str, components: Iterable[str]) -> str:
    """Render the BusyBox init script for a generated distribution."""
    validate_profile_name(name)
    selected = normalize_components(components)
    if "busybox" not in selected:
        raise ComposerError("a BusyBox init script requires the busybox component")
    titles = ", ".join(component_titles(selected))
    return "\n".join(
        (
            "#!/bin/ash",
            "",
            "export HOME=/",
            "export PATH=/bin:/usr/bin",
            "export PS1='nanvix# '",
            "",
            f'echo "Nanvix distribution {name} is ready"',
            f'echo "Included: {titles}"',
            "exec /bin/busybox ash",
            "",
        )
    )


def write_distribution(
    root: Path,
    name: str,
    components: Iterable[str],
) -> GeneratedDistribution:
    """Write a reusable profile and any required init script."""
    validate_profile_name(name)
    selected = normalize_components(components)
    root = root.resolve()
    directory = root / name
    resolved_directory = directory.resolve(strict=False)
    if not resolved_directory.is_relative_to(root):
        raise ComposerError(f"distribution path escapes {root}: {directory}")

    profile_path = _safe_output_path(directory, Path("profile.toml"))
    init_candidate = _safe_output_path(directory, Path("rootfs") / "init")
    init_path = init_candidate if "busybox" in selected else None
    _atomic_write(profile_path, render_profile(name, selected))
    if init_path is not None:
        _atomic_write(init_path, render_init(name, selected))
        init_path.chmod(0o755)
    else:
        init_candidate.unlink(missing_ok=True)
    return GeneratedDistribution(
        name=name,
        directory=directory,
        profile_path=profile_path,
        init_path=init_path,
        components=selected,
    )


def _safe_output_path(directory: Path, relative: Path) -> Path:
    base = directory.resolve(strict=False)
    candidate = directory / relative
    resolved_parent = candidate.parent.resolve(strict=False)
    if not resolved_parent.is_relative_to(base):
        raise ComposerError(f"distribution output escapes {base}: {candidate}")
    return candidate


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as output:
            output.write(content)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
