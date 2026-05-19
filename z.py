#!/usr/bin/env python3

# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

"""Nanvix mono-repo build orchestrator.

Drives the full build, test, and distribution pipeline for the Nanvix
operating system and all its userspace ports.

Target configuration: Standalone mode, 256 MB memory, microvm platform.

Usage:
    python3 z.py build       Build all components in dependency order
    python3 z.py test        Run the Nanvix runtime test target
    python3 z.py menuconfig  Select and build a binary-based distribution
    python3 z.py dist <tgt>  Create a distribution image from a named or TOML profile
    python3 z.py run <name>  Run a built distribution with the host hypervisor
    python3 z.py distclean   Remove all build artifacts and reset workspace
    python3 z.py upgrade     Update the distro, including its SDK and all modules
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from nanvix_distro.composer import (
    ComposerError,
    component_titles,
    components_from_profile,
    normalize_components,
    select_components,
    write_distribution,
)
from nanvix_distro.image import (
    ArtifactRoots,
    BuildArtifactNotFoundError,
    ImageError,
    prepare_image,
)
from nanvix_distro.profile import ProfileError, load_profile, validate_profile_name
from nanvix_distro.sdk import (
    ContractError,
    FetchedSDKContract,
    SDKContract,
    fetch_sdk_contract,
)

# ===========================================================================
# Constants
# ===========================================================================

REPO_ROOT = Path(__file__).parent.resolve()
BUILD_DIR = REPO_ROOT / "build"
RUNTIME_SYSROOT_DIR = BUILD_DIR / "sysroot"
DEPS_DIR = BUILD_DIR / "deps"
DIST_DIR = BUILD_DIR / "dist"
STAGING_DIR = BUILD_DIR / "staging"
PROFILES_DIR = REPO_ROOT / "profiles"
DISTRIBUTIONS_DIR = REPO_ROOT / "distributions"
SDK_CONTRACT_PATH = REPO_ROOT / "config" / "sdk-release.json"
IS_WINDOWS = os.name == "nt"

# Path to the local zutils source (used instead of PyPI releases)
ZUTILS_SRC = REPO_ROOT / "usr" / "lib" / "zutils" / "src"

# Build configuration
MACHINE = "microvm"
MODE = "standalone"
MEMORY = "256"
NANVIX_CP_CMD = "cp -f --preserve=timestamps"


def _filesystem_type(path: Path) -> str | None:
    """Return the Linux filesystem type containing *path*, when detectable."""
    if IS_WINDOWS or shutil.which("findmnt") is None:
        return None
    result = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "--target", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _external_nanvix_objects_dir() -> Path | None:
    """Return an ext4-backed Cargo output directory for Windows-mounted WSL worktrees."""
    if _filesystem_type(REPO_ROOT) not in {"9p", "drvfs"}:
        return None
    workspace_id = hashlib.sha256(str(REPO_ROOT).encode()).hexdigest()[:12]
    return Path.home() / ".cache" / "nanvix-distro" / workspace_id / "target"


def nanvix_make_args(*, build: bool = True) -> list[str]:
    """Return host-appropriate arguments for the Nanvix build system."""
    sysroot = RUNTIME_SYSROOT_DIR.as_posix() if IS_WINDOWS else str(RUNTIME_SYSROOT_DIR)
    arguments = [
        f"MACHINE={MACHINE}",
        f"DEPLOYMENT_MODE={MODE}",
        f"MEMORY_SIZE={MEMORY}",
    ]
    if build:
        arguments.extend(["LOG_LEVEL=error", f"SYSROOT_DIR={sysroot}"])
    if not IS_WINDOWS:
        arguments.append(f"CP_CMD={NANVIX_CP_CMD}")
        external_objects = _external_nanvix_objects_dir()
        if external_objects is not None:
            arguments.extend(["SCCACHE=", "RUSTC_WRAPPER="])
            arguments.extend(
                [
                    f"OBJECTS_DIR={external_objects}",
                    f"CARGO_TARGET_DIR={external_objects}",
                ]
            )
    return arguments


# ===========================================================================
# Dependency DAG
# ===========================================================================

# Each entry: name -> (path_relative_to_repo, [dependency_names])
PORTS: dict[str, tuple[str, list[str]]] = {
    "zlib": ("usr/bin/zlib", []),
    "bzip2": ("usr/bin/bzip2", []),
    "xz": ("usr/bin/xz", []),
    "openssl": ("usr/bin/openssl", []),
    "libffi": ("usr/lib/libffi", []),
    "busybox": ("usr/bin/busybox", []),
    "quickjs": ("usr/bin/quickjs", []),
    "sqlite": ("usr/bin/sqlite", ["zlib"]),
    "libxml2": ("usr/lib/libxml2", ["zlib"]),
    "libxslt": ("usr/lib/libxslt", ["libxml2", "zlib"]),
    "lxml": ("usr/lib/lxml", ["zlib", "libxml2", "libxslt"]),
    "cpython": (
        "usr/bin/cpython",
        [
            "zlib",
            "sqlite",
            "openssl",
            "bzip2",
            "libffi",
            "libxml2",
            "libxslt",
            "lxml",
            "xz",
        ],
    ),
}

# CPython stages a runtime tree below its release directory.
PORT_EXPORT_ROOTS: dict[str, str] = {"cpython": "sysroot-pkg"}

# ===========================================================================
# Terminal colors
# ===========================================================================

_RESET = "\033[0m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BOLD = "\033[1m"


def _supports_color() -> bool:
    """Return True if stdout supports ANSI color codes."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_COLOR = _supports_color()


def _c(color: str, text: str) -> str:
    if _COLOR:
        return f"{color}{text}{_RESET}"
    return text


# ===========================================================================
# Logging
# ===========================================================================


def info(msg: str) -> None:
    print(f"  {msg}")


def step(current: int, total: int, msg: str) -> None:
    print(f"{_c(_BOLD, f'[{current}/{total}]')} {_c(_YELLOW, msg)}")


def success(msg: str) -> None:
    print(f"{_c(_GREEN, '✓')} {msg}")


def error(msg: str) -> None:
    print(f"{_c(_RED, '✗')} {msg}", file=sys.stderr)


# ===========================================================================
# Topological sort
# ===========================================================================


def topological_sort(graph: dict[str, tuple[str, list[str]]]) -> list[str]:
    """Return port names in build order (Kahn's algorithm)."""
    in_degree: dict[str, int] = {name: 0 for name in graph}
    adjacency: dict[str, list[str]] = {name: [] for name in graph}

    for name, (_, deps) in graph.items():
        for dep in deps:
            if dep in graph:
                in_degree[name] += 1
                adjacency[dep].append(name)

    queue: deque[str] = deque(name for name, degree in in_degree.items() if degree == 0)
    result: list[str] = []

    while queue:
        node = queue.popleft()
        result.append(node)
        for neighbor in adjacency[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(graph):
        missing = set(graph.keys()) - set(result)
        error(f"Dependency cycle detected involving: {', '.join(sorted(missing))}")
        sys.exit(1)

    return result


# ===========================================================================
# Build helpers
# ===========================================================================


def _docker_available() -> bool:
    """Return True only if Docker CLI exists and the daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    return result.returncode == 0


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    verbose: bool = False,
    dry_run: bool = False,
    interactive: bool = False,
) -> None:
    """Run a subprocess, failing on error."""
    if dry_run:
        info(f"  [dry-run] $ {' '.join(cmd)}")
        return

    if verbose:
        info(f"  $ {' '.join(cmd)}")

    subprocess_env: dict[str, str] | None = None
    if env is not None:
        subprocess_env = {**os.environ, **env}

    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=subprocess_env,
        capture_output=not verbose and not interactive,
        text=True,
    )

    if result.returncode != 0:
        error(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
        if not verbose and result.stdout:
            print(result.stdout[-2000:], file=sys.stderr)
        if not verbose and result.stderr:
            print(result.stderr[-2000:], file=sys.stderr)
        sys.exit(result.returncode)


def load_sdk_contract() -> SDKContract:
    """Load the distro's pinned SDK release contract or exit with a diagnostic."""
    try:
        return SDKContract.load(SDK_CONTRACT_PATH)
    except ContractError as exc:
        error(str(exc))
        sys.exit(1)


def validate_sdk_release_set(contract: SDKContract) -> None:
    """Ensure runtime and port checkouts all match the pinned SDK contract."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT / "nanvix",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error(f"Cannot read Nanvix submodule commit: {result.stderr.strip()}")
        sys.exit(result.returncode)
    if result.stdout.strip() != contract.nanvix_commit:
        error(
            "Nanvix submodule does not match the SDK contract: "
            f"expected {contract.nanvix_commit}, found {result.stdout.strip()}"
        )
        sys.exit(1)

    consumers = [REPO_ROOT / spec[0] for spec in PORTS.values()]
    try:
        for consumer in consumers:
            contract.validate_port(consumer)
    except ContractError as exc:
        error(str(exc))
        sys.exit(1)


def build_nanvix_core(*, verbose: bool = False, dry_run: bool = False) -> None:
    """Build and install the Nanvix runtime from the contract-pinned source."""
    run_cmd(
        [
            sys.executable,
            "z.py",
            "build",
            "--release",
            "--",
            *nanvix_make_args(),
            "all",
            "install",
        ],
        cwd=REPO_ROOT / "nanvix",
        verbose=verbose,
        dry_run=dry_run,
    )


def _port_export_sources(name: str, port_dir: Path) -> list[Path]:
    """Resolve a port's canonical artifact staging trees."""
    output = port_dir / ".nanvix" / "out"
    split_sources = [output / "staging" / slot for slot in ("regular", "dev")]
    populated_sources = [
        source for source in split_sources if source.is_dir() and any(source.iterdir())
    ]
    if populated_sources:
        return populated_sources

    legacy_source = output / "release"
    if name in PORT_EXPORT_ROOTS:
        legacy_source /= PORT_EXPORT_ROOTS[name]
    return [legacy_source]


def export_port_artifacts(name: str, port_dir: Path, *, dry_run: bool = False) -> None:
    """Export a port's canonical artifact staging trees into build/deps."""
    sources = _port_export_sources(name, port_dir)
    destination = DEPS_DIR / name

    if dry_run:
        for source in sources:
            info(f"  [dry-run] Export {source} -> {destination}")
        return

    files: dict[Path, Path] = {}
    for source in sources:
        if not source.is_dir():
            error(f"{name} did not produce its artifact staging directory: {source}")
            sys.exit(1)
        for path in source.rglob("*"):
            if not path.is_file() and not path.is_symlink():
                continue
            relative = path.relative_to(source)
            if relative in files:
                error(f"{name} produced conflicting staged artifact: {relative}")
                sys.exit(1)
            files[relative] = path

    if not files:
        error(f"{name} artifact staging directories are empty")
        sys.exit(1)

    if destination.exists():
        shutil.rmtree(destination)
    for relative, source in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    info(f"  Exported {len(files)} artifact(s) to {destination}")


def stage_local_dependency_archives(
    name: str,
    port_dir: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Expose local dependency exports in the release-cache format used by port hooks."""
    _, dependencies = PORTS[name]
    if not dependencies:
        return

    cache_dir = port_dir / ".nanvix" / "cache"
    for dependency in dependencies:
        source = DEPS_DIR / dependency
        legacy_archive = (
            cache_dir / f"{dependency}-{MACHINE}-{MODE}-{MEMORY}mb.zz-local.tar.gz"
        )
        canonical_archive = (
            cache_dir / f"{dependency}-zz-local-{MACHINE}-{MODE}-{MEMORY}mb-dev.tar.gz"
        )
        if dry_run:
            info(f"  [dry-run] Archive {source} -> {legacy_archive}")
            info(f"  [dry-run] Archive {source} -> {canonical_archive}")
            continue
        if not source.is_dir() or not any(source.iterdir()):
            error(f"Local dependency export is missing or empty: {source}")
            sys.exit(1)

        cache_dir.mkdir(parents=True, exist_ok=True)
        for stale in cache_dir.glob(f"{dependency}-*zz-local*"):
            stale.unlink()
        temporary = canonical_archive.with_name(f".{canonical_archive.name}.tmp")
        temporary.unlink(missing_ok=True)
        with tarfile.open(temporary, "w:gz") as output:
            for item in sorted(source.iterdir(), key=lambda path: path.name):
                output.add(item, arcname=item.name)
        os.replace(temporary, canonical_archive)
        shutil.copy2(canonical_archive, legacy_archive)


def prepare_port_overlay_root(*, dry_run: bool = False) -> None:
    """Expose package exports under the runtime tree expected by zutils."""
    dependency_link = RUNTIME_SYSROOT_DIR / "deps"
    relative_target = os.path.relpath(DEPS_DIR, dependency_link.parent)

    if dry_run:
        info(f"  [dry-run] Link {dependency_link} -> {relative_target}")
        return

    DEPS_DIR.mkdir(parents=True, exist_ok=True)
    if dependency_link.exists():
        if dependency_link.resolve() == DEPS_DIR.resolve():
            return
        if dependency_link.is_dir() and not any(dependency_link.iterdir()):
            dependency_link.rmdir()
        else:
            error(f"Cannot expose local dependencies: {dependency_link} already exists")
            sys.exit(1)
    elif dependency_link.is_symlink():
        dependency_link.unlink()

    if IS_WINDOWS:
        result = subprocess.run(
            [
                "cmd",
                "/c",
                "mklink",
                "/J",
                str(dependency_link),
                str(DEPS_DIR.resolve()),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error(
                f"Cannot link {dependency_link} to {DEPS_DIR}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            sys.exit(result.returncode)
    else:
        dependency_link.symlink_to(relative_target, target_is_directory=True)


def clear_external_port_sysroot(port_dir: Path, *, dry_run: bool = False) -> None:
    """Discard legacy external sysroot state while preserving other settings."""
    env_path = port_dir / ".nanvix" / "env.json"
    if not env_path.is_file():
        return

    try:
        loaded_config: object = json.loads(env_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        error(f"Cannot read {env_path}: {exc}")
        sys.exit(1)
    if not isinstance(loaded_config, dict):
        error(f"Invalid port environment in {env_path}: expected a JSON object")
        sys.exit(1)
    config = cast(dict[str, object], loaded_config)

    key = "NANVIX_SYSROOT"
    configured = config.get(key)
    if not isinstance(configured, str):
        return
    configured_path = Path(configured)
    if not configured_path.is_absolute():
        configured_path = port_dir / configured_path
    local_sysroot = port_dir / ".nanvix" / "sysroot"
    if configured_path.resolve() == local_sysroot.resolve():
        return

    if dry_run:
        info(f"  [dry-run] Remove legacy {key} from {env_path}")
        return

    del config[key]
    temporary = env_path.with_name(f".{env_path.name}.tmp")
    temporary.write_text(f"{json.dumps(config, indent=2)}\n", encoding="utf-8")
    os.replace(temporary, env_path)


def build_port(
    name: str,
    *,
    verbose: bool = False,
    dry_run: bool = False,
) -> None:
    """Build one port with its SDK-era zutils hook and local dependencies."""
    path, _ = PORTS[name]
    port_dir = REPO_ROOT / path
    build_script = port_dir / ".nanvix" / "z.py"
    env = {
        "PYTHONPATH": str(ZUTILS_SRC),
        "NANVIX_MACHINE": MACHINE,
        "NANVIX_DEPLOYMENT_MODE": MODE,
        "NANVIX_MEMORY_SIZE": f"{MEMORY}mb",
    }

    stage_local_dependency_archives(
        name,
        port_dir,
        dry_run=dry_run,
    )
    clear_external_port_sysroot(port_dir, dry_run=dry_run)
    run_cmd(
        [
            sys.executable,
            str(build_script),
            "setup",
            "--offline",
            "--with-nanvix",
            str(RUNTIME_SYSROOT_DIR),
        ],
        cwd=port_dir,
        env=env,
        verbose=verbose,
        dry_run=dry_run,
    )
    run_cmd(
        [sys.executable, str(build_script), "build"],
        cwd=port_dir,
        env=env,
        verbose=verbose,
        dry_run=dry_run,
    )
    export_port_artifacts(name, port_dir, dry_run=dry_run)


# ===========================================================================
# Distro upgrade helpers
# ===========================================================================


def _git_output(args: list[str], cwd: Path) -> str:
    """Return stdout of a git command, raising on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error(
            f"git {' '.join(args)} (cwd={cwd}) failed: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        sys.exit(result.returncode)
    return result.stdout


def list_submodule_paths(repo: Path) -> list[str]:
    """Return submodule paths (relative to *repo*) declared in *repo*/.gitmodules."""
    return [path for _, path, _ in list_submodules(repo)]


def list_submodules(repo: Path) -> list[tuple[str, str, str]]:
    """Return submodule entries (name, path, url) from *repo*/.gitmodules."""
    gitmodules = repo / ".gitmodules"
    if not gitmodules.exists():
        return []
    out = _git_output(
        [
            "config",
            "-f",
            ".gitmodules",
            "--get-regexp",
            r"^submodule\..*\.(path|url)$",
        ],
        cwd=repo,
    )
    entries: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        # key: submodule.<name>.<field>
        parts = key.split(".")
        if len(parts) < 3:
            continue
        name = ".".join(parts[1:-1])
        field = parts[-1]
        entries.setdefault(name, {})[field] = value.strip()
    result: list[tuple[str, str, str]] = []
    for name, kv in entries.items():
        path = kv.get("path")
        url = kv.get("url")
        if path and url:
            result.append((name, path, url))
    return result


@dataclass(frozen=True)
class UpgradeTarget:
    """An exact top-level submodule release selected during preflight."""

    path: str
    ref: str
    commit: str


class IncompleteReleaseSetError(ContractError):
    """The latest SDK release has not reached every SDK-consuming port."""


def _upgrade_order(repo: Path) -> list[str]:
    """Return top-level submodules in deterministic dependency-aware order."""
    submodules = list_submodule_paths(repo)
    port_paths = {name: spec[0] for name, spec in PORTS.items()}
    port_order = list(reversed(topological_sort(PORTS)))
    ordered_ports = [
        port_paths[name] for name in port_order if port_paths[name] in submodules
    ]
    extras = [path for path in submodules if path not in ordered_ports]
    return ordered_ports + extras


def _latest_matching_tag(submodule: Path, pattern: str) -> tuple[str, str]:
    """Return the highest version-sorted tag and its peeled commit."""
    output = _git_output(
        ["tag", "--list", pattern, "--sort=-v:refname"],
        cwd=submodule,
    )
    tags = [tag.strip() for tag in output.splitlines() if tag.strip()]
    if not tags:
        raise ContractError(f"{submodule}: no release tag matches {pattern!r}")
    tag = tags[0]
    commit = _git_output(["rev-list", "-n", "1", tag], cwd=submodule).strip()
    return tag, commit


def _verify_commit(submodule: Path, commit: str) -> None:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=submodule,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError(f"{submodule}: required commit is unavailable: {commit}")


def _is_ancestor(submodule: Path, ancestor: str, descendant: str) -> bool:
    """Return whether *ancestor* is reachable from *descendant*."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=submodule,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode in (0, 1):
        return result.returncode == 0
    detail = result.stderr.strip() or result.stdout.strip()
    raise ContractError(
        f"{submodule}: cannot compare commits {ancestor} and {descendant}: {detail}"
    )


def _reject_downgrade(submodule: Path, target: UpgradeTarget) -> None:
    """Reject a release target that precedes the currently pinned commit."""
    current = _git_output(
        ["rev-parse", f"HEAD:{target.path}"],
        cwd=REPO_ROOT,
    ).strip()
    if current == target.commit or not _is_ancestor(submodule, target.commit, current):
        return
    raise ContractError(
        f"{target.path}: refusing to downgrade from current pin {current} "
        f"to {target.ref} ({target.commit}); publish a release containing the "
        "current pin or rerun with --allow-downgrade"
    )


def _validate_port_at_commit(
    contract: SDKContract,
    submodule: Path,
    path: str,
    commit: str,
) -> None:
    manifest = _git_output(
        ["show", f"{commit}:.nanvix/nanvix.toml"],
        cwd=submodule,
    )
    lock = _git_output(
        ["show", f"{commit}:.nanvix/nanvix.lock"],
        cwd=submodule,
    )
    contract.validate_port_text(manifest, lock, source=f"{path}@{commit[:12]}")


def resolve_upgrade_targets(
    contract: SDKContract,
    *,
    verbose: bool,
    allow_downgrade: bool = False,
) -> dict[str, UpgradeTarget]:
    """Resolve and validate every top-level gitlink without changing checkouts."""
    sdk_consumers = {spec[0] for spec in PORTS.values()}
    targets: dict[str, UpgradeTarget] = {}

    for _, path, _ in list_submodules(REPO_ROOT):
        submodule = REPO_ROOT / path
        if not (submodule / ".git").exists():
            info(f"  initializing {path}")
            run_cmd(
                ["git", "submodule", "update", "--init", "--", path],
                cwd=REPO_ROOT,
                verbose=verbose,
            )

        run_cmd(
            ["git", "fetch", "--tags", "--force", "origin"],
            cwd=submodule,
            verbose=verbose,
        )

        if path == "nanvix":
            ref = contract.nanvix_tag
            commit = contract.nanvix_commit
            _verify_commit(submodule, commit)
        elif path == "usr/lib/zutils":
            ref, commit = _latest_matching_tag(submodule, "v[0-9]*")
        elif path in sdk_consumers:
            pattern = f"*-nanvix-{contract.release_coordinate}"
            try:
                ref, commit = _latest_matching_tag(submodule, pattern)
            except ContractError as exc:
                raise IncompleteReleaseSetError(str(exc)) from exc
            _validate_port_at_commit(contract, submodule, path, commit)
        else:
            raise ContractError(f"no coherent upgrade policy is defined for {path}")

        target = UpgradeTarget(path=path, ref=ref, commit=commit)
        if not allow_downgrade:
            _reject_downgrade(submodule, target)
        targets[path] = target
        info(f"  {path}: {target.ref} ({target.commit[:10]})")

    return targets


def upgrade_submodule(target: UpgradeTarget, *, verbose: bool) -> None:
    """Check out and stage one preflighted top-level gitlink."""
    submodule = REPO_ROOT / target.path
    current = _git_output(["rev-parse", "HEAD"], cwd=submodule).strip()
    if current == target.commit:
        info(f"  already at {target.ref} ({target.commit[:10]})")
    else:
        info(f"  checking out {target.ref} ({target.commit[:10]})")
        run_cmd(
            ["git", "checkout", "--detach", target.commit],
            cwd=submodule,
            verbose=verbose,
        )
    run_cmd(
        ["git", "add", "--", target.path],
        cwd=REPO_ROOT,
        verbose=verbose,
    )


# ===========================================================================
# Commands
# ===========================================================================


def cmd_build(args: argparse.Namespace) -> None:
    """Build all components in dependency order."""
    verbose = args.verbose
    dry_run = args.dry_run
    contract = load_sdk_contract()
    validate_sdk_release_set(contract)
    info(f"SDK: {contract.sdk_version} ({contract.image_ref})")

    if not dry_run:
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_SYSROOT_DIR.mkdir(parents=True, exist_ok=True)
        DEPS_DIR.mkdir(parents=True, exist_ok=True)

    build_order = topological_sort(PORTS)

    if build_order and not dry_run and not _docker_available():
        error("Docker is required to build SDK-based userspace ports")
        sys.exit(1)

    total = len(build_order) + 1

    step(1, total, "Building nanvix (core OS)...")
    build_nanvix_core(verbose=verbose, dry_run=dry_run)
    success("nanvix core built")

    if build_order:
        prepare_port_overlay_root(dry_run=dry_run)

    for i, port_name in enumerate(build_order, start=2):
        step(i, total, f"Building {port_name}...")
        build_port(port_name, verbose=verbose, dry_run=dry_run)
        success(f"{port_name} built")

    success("All components built successfully")


def cmd_test(args: argparse.Namespace) -> None:
    """Run the Nanvix runtime test target."""
    verbose = args.verbose
    dry_run = args.dry_run
    validate_sdk_release_set(load_sdk_contract())

    step(1, 1, "Testing nanvix (core OS)...")
    run_cmd(
        [
            sys.executable,
            "z.py",
            "test",
            "--",
            *nanvix_make_args(build=False),
        ],
        cwd=REPO_ROOT / "nanvix",
        verbose=verbose,
        dry_run=dry_run,
    )
    success("Nanvix runtime tests passed")


def cmd_menuconfig(args: argparse.Namespace) -> None:
    """Select components, save a reusable profile, and build its image."""
    name = args.name

    try:
        validate_profile_name(name)
        if (PROFILES_DIR / f"{name}.toml").is_file():
            raise ComposerError(
                f"{name!r} is a reserved built-in profile name; choose another name"
            )
        profile_path = DISTRIBUTIONS_DIR / name / "profile.toml"
        if args.include is None:
            initial = components_from_profile(profile_path)
            selected = select_components(name, initial)
        else:
            selected = normalize_components(args.include)
    except (ComposerError, ProfileError) as exc:
        error(str(exc))
        sys.exit(1)

    if selected is None:
        info("Distribution configuration cancelled.")
        return

    selected_names = ", ".join(component_titles(selected))
    if args.dry_run:
        info(f"[dry-run] Distribution: {name}")
        info(f"[dry-run] Components: {selected_names}")
        info(f"[dry-run] Would write: {profile_path}")
        info(f"[dry-run] Would build: {DIST_DIR / name}")
        return

    try:
        generated = write_distribution(DISTRIBUTIONS_DIR, name, selected)
    except (ComposerError, ProfileError, OSError) as exc:
        error(f"Cannot save distribution configuration: {exc}")
        sys.exit(1)

    success(f"Saved distribution profile at {generated.profile_path}")
    info(f"  Components: {selected_names}")
    cmd_dist(
        argparse.Namespace(
            target=str(generated.profile_path),
            verbose=args.verbose,
            dry_run=False,
        )
    )


def cmd_dist(args: argparse.Namespace) -> None:
    """Create a distribution image from a named or explicit TOML profile."""
    target = args.target
    verbose = args.verbose
    dry_run = args.dry_run
    named_profile = PROFILES_DIR / f"{target}.toml"
    if named_profile.is_file():
        profile_path = named_profile
    else:
        profile_path = Path(target).expanduser()
        if not profile_path.is_absolute():
            profile_path = Path.cwd() / profile_path
        profile_path = profile_path.resolve()
        if not profile_path.is_file():
            available = ", ".join(
                path.stem for path in sorted(PROFILES_DIR.glob("*.toml"))
            )
            error(f"Distribution profile not found: {target}")
            error(f"Named profiles: {available}")
            sys.exit(1)

    validate_sdk_release_set(load_sdk_contract())
    try:
        profile = load_profile(profile_path)
        plan = prepare_image(
            profile,
            profile_path,
            ArtifactRoots(runtime=RUNTIME_SYSROOT_DIR, packages=DEPS_DIR),
            DIST_DIR,
            STAGING_DIR,
            dry_run=dry_run,
        )
    except (ProfileError, ImageError) as exc:
        error(str(exc))
        if isinstance(exc, BuildArtifactNotFoundError):
            error(
                "Build distribution prerequisites first with: "
                f"{sys.executable} {REPO_ROOT / 'z.py'} build"
            )
        sys.exit(1)

    step(1, 4, f"Creating {profile.name} initrd...")
    run_cmd(
        list(plan.mkimage_command),
        verbose=verbose,
        dry_run=dry_run,
    )

    step(2, 4, f"Creating {profile.name} ramfs...")
    if plan.mkramfs_command is not None:
        run_cmd(
            list(plan.mkramfs_command),
            verbose=verbose,
            dry_run=dry_run,
        )
    else:
        assert plan.ramfs_source is not None
        if dry_run:
            info(f"  [dry-run] Copy {plan.ramfs_source} -> {plan.ramfs}")
        else:
            shutil.copy2(plan.ramfs_source, plan.ramfs)

    for index, (source, destination) in enumerate(plan.copies, start=3):
        step(index, 4, f"Copying {source.name}...")
        if dry_run:
            info(f"  [dry-run] Copy {source} -> {destination}")
        else:
            shutil.copy2(source, destination)

    success(f"Distribution image created at {plan.dist_dir}")
    info(f"  Run: {sys.executable} {REPO_ROOT / 'z.py'} run {profile.name}")


def distribution_run_command(executable: Path | None = None) -> list[str]:
    """Return the host-specific command for a prepared distribution."""
    command = [
        (
            str(executable)
            if executable is not None
            else (".\\nanvixd.exe" if IS_WINDOWS else "./nanvixd.elf")
        )
    ]
    if not IS_WINDOWS:
        command.extend(["-console-file", "/dev/stdout"])
    command.extend(
        [
            "-bin-dir",
            ".\\bin" if IS_WINDOWS else "./bin",
            "-ramfs",
            ".\\bin\\nanvix.ramfs" if IS_WINDOWS else "./bin/nanvix.ramfs",
            "--",
            ".\\bin\\nanvix.initrd" if IS_WINDOWS else "./bin/nanvix.initrd",
        ]
    )
    return command


def cmd_run(args: argparse.Namespace) -> None:
    """Run a built distribution with WHP on Windows or KVM on Linux."""
    try:
        validate_profile_name(args.target)
    except ProfileError as exc:
        error(str(exc))
        sys.exit(1)

    dist_dir = DIST_DIR / args.target
    host_binary = dist_dir / ("nanvixd.exe" if IS_WINDOWS else "nanvixd.elf")
    if not args.dry_run:
        required = (
            host_binary,
            dist_dir / "bin" / "kernel.elf",
            dist_dir / "bin" / "nanvix.initrd",
            dist_dir / "bin" / "nanvix.ramfs",
        )
        for path in required:
            if not path.is_file():
                error(f"Distribution artifact not found: {path}")
                error(
                    f"Build it first with: {sys.executable} "
                    f"{REPO_ROOT / 'z.py'} dist {args.target}"
                )
                sys.exit(1)

    run_cmd(
        distribution_run_command(host_binary.resolve()),
        cwd=dist_dir,
        verbose=args.verbose,
        dry_run=args.dry_run,
        interactive=True,
    )


def cmd_distclean(args: argparse.Namespace) -> None:
    """Remove all build artifacts and reset workspace."""
    verbose = args.verbose
    dry_run = args.dry_run

    step(1, 3, "Removing build/ directory...")
    build_directories = [BUILD_DIR]
    external_objects = _external_nanvix_objects_dir()
    if external_objects is not None:
        build_directories.append(external_objects)
    for directory in build_directories:
        if not directory.exists():
            continue
        if not dry_run:
            shutil.rmtree(directory)
        else:
            info(f"  [dry-run] rm -rf {directory}")
    success("build artifacts removed")

    step(2, 3, "Cleaning submodules...")
    if dry_run:
        info("  [dry-run] Remove CPython external build volume")
    else:
        _clean_cpython_external_state()
    run_cmd(
        ["git", "submodule", "foreach", "--recursive", "git", "clean", "-fdx"],
        cwd=REPO_ROOT,
        verbose=verbose,
        dry_run=dry_run,
    )
    success("Submodules cleaned")

    step(3, 3, "Cleaning top-level repo...")
    run_cmd(
        [
            "git",
            "clean",
            "-fdx",
            "--exclude=.venv",
            "--exclude=*.py",
            "--exclude=doc/",
            "--exclude=distributions/",
        ],
        cwd=REPO_ROOT,
        verbose=verbose,
        dry_run=dry_run,
    )
    success("Workspace cleaned")


def _clean_cpython_external_state() -> None:
    """Remove CPython build state stored outside its submodule worktree."""
    helper = REPO_ROOT / "usr" / "bin" / "cpython" / ".nanvix" / "_docker.py"
    config_path = helper.with_name("config.py")
    if not helper.is_file() or not config_path.is_file():
        return

    config_spec = importlib.util.spec_from_file_location(
        "nanvix_distro_cpython_config", config_path
    )
    helper_spec = importlib.util.spec_from_file_location(
        "nanvix_distro_cpython_docker", helper
    )
    if (
        config_spec is None
        or config_spec.loader is None
        or helper_spec is None
        or helper_spec.loader is None
    ):
        return

    config_module = importlib.util.module_from_spec(config_spec)
    config_spec.loader.exec_module(config_module)
    helper_module = importlib.util.module_from_spec(helper_spec)
    previous_config = sys.modules.get("config")
    try:
        sys.modules["config"] = config_module
        helper_spec.loader.exec_module(helper_module)
    finally:
        if previous_config is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous_config
    remove_volume = getattr(helper_module, "remove_build_volume", None)
    if callable(remove_volume):
        remove_volume(helper.parent.parent)


def cmd_upgrade(args: argparse.Namespace) -> None:
    """Upgrade the distro, including its SDK and all modules, as one release set."""
    verbose = args.verbose
    dry_run = args.dry_run
    order = _upgrade_order(REPO_ROOT)
    if not order:
        info("No distro modules to upgrade.")
        return

    info("Preflight: resolving the distro release set...")
    try:
        fetched: FetchedSDKContract = fetch_sdk_contract(
            args.sdk_version,
            token=os.environ.get("GH_TOKEN"),
        )
        info(
            f"  SDK: {fetched.contract.sdk_version} "
            f"({fetched.contract.image_digest})"
        )
        targets = resolve_upgrade_targets(
            fetched.contract,
            verbose=verbose,
            allow_downgrade=args.allow_downgrade,
        )
    except ContractError as exc:
        if isinstance(exc, IncompleteReleaseSetError) and getattr(
            args, "defer_incomplete_release_set", False
        ):
            info(f"Distro release set is still propagating: {exc}")
            info("Upgrade deferred; no tracked changes were applied.")
            return
        error(f"Distro upgrade preflight failed: {exc}")
        error("No tracked changes were applied.")
        sys.exit(1)

    missing = [path for path in order if path not in targets]
    if missing:
        error(f"Upgrade preflight did not resolve: {', '.join(missing)}")
        sys.exit(1)
    success("Preflight passed: the SDK and every module match the release set.")

    if dry_run:
        info("Dry-run: would update the distro release set:")
        info(f"  {SDK_CONTRACT_PATH} -> {fetched.contract.sdk_version}")
        for path in order:
            target = targets[path]
            info(f"  {path} -> {target.ref} ({target.commit[:10]})")
        return

    total = len(order)
    for idx, path in enumerate(order, start=1):
        step(idx, total, f"Upgrading {path}")
        upgrade_submodule(targets[path], verbose=verbose)
        success(f"{path} upgraded")

    fetched.write(SDK_CONTRACT_PATH)
    run_cmd(
        ["git", "add", "--", str(SDK_CONTRACT_PATH.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        verbose=verbose,
    )
    info("")
    info("The distro SDK and all modules are coherent. Review staged changes with:")
    info("  git status")
    info("  git diff --staged")


# ===========================================================================
# CLI
# ===========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="z.py",
        description="Nanvix mono-repo build orchestrator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show full subprocess output"
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Print the build plan without executing",
    )

    subparsers = parser.add_subparsers(dest="command")

    # build
    subparsers.add_parser("build", help="Build all components in dependency order")

    # test
    subparsers.add_parser("test", help="Run the Nanvix runtime test target")

    # menuconfig
    menuconfig_parser = subparsers.add_parser(
        "menuconfig",
        help="Interactively compose and build a distribution",
    )
    menuconfig_parser.add_argument(
        "name",
        help="Distribution name (must not be busybox, python, or javascript)",
    )
    menuconfig_parser.add_argument(
        "--include",
        nargs="+",
        metavar="COMPONENT",
        help=(
            "Bypass the menu and select components "
            "(busybox, cpython/python, quickjs/javascript, or all)"
        ),
    )

    # dist
    dist_parser = subparsers.add_parser(
        "dist", help="Create a distribution image from a named or TOML profile"
    )
    dist_parser.add_argument(
        "target",
        help="Named profile (busybox, python, javascript) or TOML profile path",
    )

    # run
    run_parser = subparsers.add_parser(
        "run", help="Run a built distribution with the host hypervisor"
    )
    run_parser.add_argument("target", help="Built distribution name")

    # distclean
    subparsers.add_parser("distclean", help="Remove all build artifacts")

    # distro upgrade
    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Update the distro, including its SDK and all modules",
    )
    upgrade_parser.add_argument(
        "--sdk-version",
        metavar="TAG",
        help="Exact SDK release tag (default: latest completed release)",
    )
    upgrade_parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="Allow release selection to move submodules to older commits",
    )
    upgrade_parser.add_argument(
        "--defer-incomplete-release-set",
        action="store_true",
        help="Exit successfully when the latest SDK is still propagating to ports",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "build": cmd_build,
        "test": cmd_test,
        "menuconfig": cmd_menuconfig,
        "dist": cmd_dist,
        "run": cmd_run,
        "distclean": cmd_distclean,
        "upgrade": cmd_upgrade,
    }

    handler = dispatch[args.command]
    handler(args)


if __name__ == "__main__":
    main()
