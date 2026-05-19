"""Filesystem staging and command planning for Nanvix distribution images."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from nanvix_distro.profile import ArtifactRef, ImageProfile, RamfsEntry

HOST_EXECUTABLE_SUFFIX = ".exe" if os.name == "nt" else ".elf"


class ImageError(ValueError):
    """Raised when image inputs cannot be resolved or staged safely."""


class BuildArtifactNotFoundError(ImageError):
    """Raised when a generated runtime or package artifact is missing."""


@dataclass(frozen=True)
class ArtifactRoots:
    """Roots used to resolve runtime, package, and profile artifacts."""

    runtime: Path
    packages: Path


@dataclass(frozen=True)
class ImagePlan:
    """Commands and copies required to materialize one distribution."""

    name: str
    dist_dir: Path
    bin_dir: Path
    initrd: Path
    ramfs: Path
    mkimage_command: tuple[str, ...]
    mkramfs_command: tuple[str, ...] | None
    ramfs_source: Path | None
    copies: tuple[tuple[Path, Path], ...]


def _host_executable(bin_dir: Path, name: str) -> Path:
    """Resolve a host tool using the current platform's executable suffix."""
    return bin_dir / f"{name}{HOST_EXECUTABLE_SUFFIX}"


def _source_root(
    reference: ArtifactRef, profile_path: Path, roots: ArtifactRoots
) -> Path:
    if reference.source == "runtime":
        return roots.runtime
    if reference.source == "profile":
        return profile_path.parent
    if reference.source.startswith("package:"):
        return roots.packages / reference.source.removeprefix("package:")
    raise ImageError(f"unsupported artifact source {reference.source!r}")


def _resolve(
    reference: ArtifactRef,
    profile_path: Path,
    roots: ArtifactRoots,
    *,
    required: bool,
) -> Path:
    base = _source_root(reference, profile_path, roots).resolve()
    candidate = base.joinpath(*reference.path.parts)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(base):
        raise ImageError(f"artifact escapes its source root: {reference.path}")
    if required and not candidate.exists():
        if reference.source == "runtime" or reference.source.startswith("package:"):
            raise BuildArtifactNotFoundError(f"artifact not found: {candidate}")
        raise ImageError(f"artifact not found: {candidate}")
    return candidate


def _guest_destination(root: Path, destination: PurePosixPath) -> Path:
    parts = destination.parts[1:]
    return root.joinpath(*parts)


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _copy_entry(
    source: Path, destination: Path, *, normalize_newlines: bool = False
) -> None:
    if source.is_symlink():
        if _exists(destination):
            raise ImageError(f"RAMFS layer conflict at {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(os.readlink(source))
        return

    if source.is_dir():
        if _exists(destination) and (
            destination.is_symlink() or not destination.is_dir()
        ):
            raise ImageError(f"RAMFS layer conflict at {destination}")
        destination.mkdir(parents=True, exist_ok=True)
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            _copy_entry(child, destination / child.name)
        return

    if not source.is_file():
        raise ImageError(f"unsupported RAMFS input: {source}")
    if _exists(destination):
        raise ImageError(f"RAMFS layer conflict at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if normalize_newlines:
        destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
        shutil.copystat(source, destination)
    else:
        shutil.copy2(source, destination)


def _apply_ramfs_entry(
    entry: RamfsEntry,
    staging_root: Path,
    profile_path: Path,
    roots: ArtifactRoots,
) -> None:
    source = _resolve(entry.artifact, profile_path, roots, required=True)
    destination = _guest_destination(staging_root, entry.destination)
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            _copy_entry(child, destination / child.name)
    else:
        if entry.destination == PurePosixPath("/"):
            raise ImageError("a RAMFS file entry cannot target '/'")
        _copy_entry(
            source,
            destination,
            normalize_newlines=entry.normalize_newlines,
        )


def prepare_image(
    profile: ImageProfile,
    profile_path: Path,
    roots: ArtifactRoots,
    dist_root: Path,
    staging_root: Path,
    *,
    dry_run: bool = False,
) -> ImagePlan:
    """Resolve inputs, stage RAMFS layers, and return an executable image plan."""
    dist_dir = dist_root / profile.name
    bin_dir = dist_dir / "bin"
    initrd = bin_dir / "nanvix.initrd"
    ramfs = bin_dir / "nanvix.ramfs"

    runtime_bin = roots.runtime / "bin"
    mkimage = _host_executable(runtime_bin, "mkimage")
    mkramfs = _host_executable(runtime_bin, "mkramfs")
    kernel = runtime_bin / "kernel.elf"
    nanvixd = _host_executable(runtime_bin, "nanvixd")
    if not dry_run:
        for required in (mkimage, kernel, nanvixd):
            if not required.is_file():
                raise BuildArtifactNotFoundError(
                    f"required runtime artifact not found: {required}"
                )
        bin_dir.mkdir(parents=True, exist_ok=True)

    entries: list[str] = []
    for program in profile.programs:
        artifact = _resolve(
            program.artifact,
            profile_path,
            roots,
            required=not dry_run,
        )
        if not dry_run and not artifact.is_file():
            raise ImageError(f"program is not a file: {artifact}")
        entries.append(f"{artifact};{program.command_line}")

    mkimage_command = [str(mkimage), "-o", str(initrd)]
    if profile.kernel_args:
        mkimage_command.extend(["-k", profile.kernel_args])
    mkimage_command.extend(entries)

    ramfs_source: Path | None = None
    mkramfs_command: tuple[str, ...] | None
    if profile.ramfs_image is not None:
        ramfs_source = _resolve(
            profile.ramfs_image,
            profile_path,
            roots,
            required=not dry_run,
        )
        if not dry_run and not ramfs_source.is_file():
            raise ImageError(f"RAMFS image is not a file: {ramfs_source}")
        mkramfs_command = None
    else:
        staged_ramfs = staging_root / profile.name / "rootfs"
        if not dry_run:
            if not mkramfs.is_file():
                raise BuildArtifactNotFoundError(
                    f"required runtime artifact not found: {mkramfs}"
                )
            if staged_ramfs.exists():
                shutil.rmtree(staged_ramfs)
            staged_ramfs.mkdir(parents=True)
            for directory in profile.ramfs_directories:
                destination = _guest_destination(staged_ramfs, directory)
                if _exists(destination) and not destination.is_dir():
                    raise ImageError(f"RAMFS directory conflict at {destination}")
                destination.mkdir(parents=True, exist_ok=True)
            for entry in profile.ramfs_entries:
                _apply_ramfs_entry(entry, staged_ramfs, profile_path, roots)
        mkramfs_command = (str(mkramfs), "-o", str(ramfs), str(staged_ramfs))

    return ImagePlan(
        name=profile.name,
        dist_dir=dist_dir,
        bin_dir=bin_dir,
        initrd=initrd,
        ramfs=ramfs,
        mkimage_command=tuple(mkimage_command),
        mkramfs_command=mkramfs_command,
        ramfs_source=ramfs_source,
        copies=((kernel, bin_dir / "kernel.elf"), (nanvixd, dist_dir / nanvixd.name)),
    )
