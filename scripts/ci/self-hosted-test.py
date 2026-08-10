#!/usr/bin/env python3

# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPSConnection
from pathlib import Path
from typing import BinaryIO, NoReturn, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

IS_WINDOWS = os.name == "nt"
SCRIPT_ROOT = Path(__file__).resolve().parents[2]
PROFILES = (
    ("python", "cpython"),
    ("javascript", "quickjs"),
    ("busybox", "busybox"),
    ("ci-composed", "cpython-quickjs-busybox"),
)
RELEASE_NOTES = """\
Nanvix distribution images built from merge commit {commit}.

Included configurations:
- Linux/KVM: BusyBox
- Linux/KVM: CPython
- Linux/KVM: QuickJS
- Linux/KVM: CPython, QuickJS, and BusyBox
- Windows/WHP: BusyBox
- Windows/WHP: CPython
- Windows/WHP: QuickJS
- Windows/WHP: CPython, QuickJS, and BusyBox
"""


class CiError(RuntimeError):
    """Report a CI failure without a Python traceback."""


@dataclass(frozen=True)
class GuestPackage:
    name: str
    asset: str
    required_files: tuple[str, ...]


GUEST_PACKAGES = (
    GuestPackage(
        "busybox",
        "busybox-windows-x86-microvm-standalone-256mb.zip",
        ("bin/busybox.elf",),
    ),
    GuestPackage(
        "quickjs",
        "quickjs-windows-x86-microvm-standalone-256mb.zip",
        ("bin/qjs.elf",),
    ),
    GuestPackage(
        "cpython",
        "cpython-windows-x86-microvm-standalone-256mb.zip",
        ("bin/python.elf", "cpython-ramfs.img"),
    ),
)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CiError(f"Required environment variable is not set: {name}")
    return value


def workspace() -> Path:
    return Path(os.environ.get("GITHUB_WORKSPACE", SCRIPT_ROOT))


def require_platform(*, windows: bool) -> None:
    if IS_WINDOWS != windows:
        expected = "Windows" if windows else "Linux"
        raise CiError(f"This task requires a {expected} runner")


def append_github_file(environment_name: str, name: str, value: str) -> None:
    with Path(require_env(environment_name)).open("a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"[INFO] {shlex.join(command)}")
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=check,
        input=input_text,
        text=True,
    )


def run_output(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    print(f"[INFO] {shlex.join(command)}")
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def load_json(path: str) -> object:
    with Path(path).open(encoding="utf-8") as json_file:
        return cast(object, json.load(json_file))


def decode_json(payload: bytes, description: str) -> object:
    try:
        return cast(object, json.loads(payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CiError(f"{description} is not valid JSON") from error


def require_object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CiError(f"{description} is not a JSON object")
    return cast(dict[str, object], value)


def require_array(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise CiError(f"{description} is not a JSON array")
    return cast(list[object], value)


def require_id(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CiError(f"{description} has invalid id: {value!r}")
    return value


def require_release_id(value: str) -> int:
    if not re.fullmatch(r"[1-9]\d*", value):
        raise CiError(f"Invalid release ID: {value}")
    return int(value)


def github_headers(content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {require_env('GH_TOKEN')}",
        "User-Agent": "nanvix-distro-ci",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def github_request(
    method: str,
    url: str,
    *,
    payload: bytes | Path | None = None,
    content_type: str | None = None,
) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise CiError(f"GitHub API URL must use HTTPS: {url}")

    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    headers = github_headers(content_type)
    payload_file: BinaryIO | None = None
    if isinstance(payload, Path):
        payload_file = payload.open("rb")
        headers["Content-Length"] = str(payload.stat().st_size)
        body: bytes | BinaryIO | None = payload_file
    else:
        body = payload

    connection = HTTPSConnection(parsed.hostname, parsed.port, timeout=180)
    try:
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        response_payload = response.read()
        if not 200 <= response.status < 300:
            detail = response_payload.decode("utf-8", errors="replace")
            raise CiError(
                f"GitHub API {method} {target} failed with "
                f"HTTP {response.status}: {detail}"
            )
        return response_payload
    except OSError as error:
        raise CiError(f"GitHub API {method} {target} failed: {error}") from error
    finally:
        if payload_file is not None:
            payload_file.close()
        connection.close()


def github_json_request(
    method: str,
    url: str,
    *,
    payload: dict[str, object] | None = None,
    description: str,
) -> object:
    encoded = None if payload is None else json.dumps(payload).encode("utf-8")
    response = github_request(
        method,
        url,
        payload=encoded,
        content_type="application/json" if encoded is not None else None,
    )
    return decode_json(response, description)


def download_file(url: str, destination: Path) -> None:
    request = Request(url, headers=github_headers())
    try:
        with urlopen(request, timeout=180) as response:
            with destination.open("wb") as destination_file:
                shutil.copyfileobj(response, destination_file)
    except (HTTPError, URLError, OSError) as error:
        raise CiError(f"Cannot download {url}: {error}") from error


def has_positive_size(asset: dict[str, object]) -> bool:
    size = asset.get("size")
    return isinstance(size, int) and not isinstance(size, bool) and size > 0


def find_draft_release_value(
    releases_value: object,
    expected_tag: str,
    expected_commit: str,
) -> int | None:
    releases = require_array(releases_value, "releases response")
    matches: list[dict[str, object]] = []
    for value in releases:
        release = require_object(value, "release")
        if release.get("tag_name") == expected_tag:
            matches.append(release)

    if len(matches) > 1:
        raise CiError(
            f"expected at most one release tagged {expected_tag!r}, "
            f"found {len(matches)}"
        )
    if not matches:
        return None

    release = matches[0]
    if release.get("target_commitish") != expected_commit:
        raise CiError(
            f"release targets {release.get('target_commitish')}, "
            f"expected {expected_commit}"
        )
    if not release.get("draft"):
        raise CiError(f"release {release.get('id')} is already published")
    return require_id(release.get("id"), "release")


def find_release_asset_ids_value(
    assets_value: object,
    expected_name: str,
) -> list[int]:
    assets = require_array(assets_value, "release assets response")
    matches: list[int] = []
    for value in assets:
        asset = require_object(value, "release asset")
        if asset.get("name") == expected_name:
            matches.append(require_id(asset.get("id"), "release asset"))
    return matches


def validate_uploaded_asset_value(asset_value: object, expected_name: str) -> None:
    asset = require_object(asset_value, "uploaded asset response")
    if asset.get("name") != expected_name:
        raise CiError(f"uploaded asset has unexpected name: {asset.get('name')!r}")
    if asset.get("state") != "uploaded" or not has_positive_size(asset):
        raise CiError(f"uploaded asset is incomplete: {asset!r}")


def expected_release_assets(commit: str) -> set[str]:
    return {
        f"nanvix-distro-linux-x86-microvm-256mb-busybox-{commit}.tar.gz",
        f"nanvix-distro-linux-x86-microvm-256mb-cpython-{commit}.tar.gz",
        f"nanvix-distro-linux-x86-microvm-256mb-cpython-quickjs-busybox-{commit}.tar.gz",
        f"nanvix-distro-linux-x86-microvm-256mb-quickjs-{commit}.tar.gz",
        f"nanvix-distro-windows-x86-microvm-256mb-busybox-{commit}.zip",
        f"nanvix-distro-windows-x86-microvm-256mb-cpython-{commit}.zip",
        f"nanvix-distro-windows-x86-microvm-256mb-cpython-quickjs-busybox-{commit}.zip",
        f"nanvix-distro-windows-x86-microvm-256mb-quickjs-{commit}.zip",
    }


def validate_release_value(
    release_value: object,
    expected_tag: str,
    expected_commit: str,
    *,
    published: bool,
) -> None:
    release = require_object(release_value, "release response")
    qualifier = "published release" if published else "release"
    if release.get("tag_name") != expected_tag:
        raise CiError(f"{qualifier} has unexpected tag: {release.get('tag_name')!r}")
    if release.get("target_commitish") != expected_commit:
        raise CiError(
            f"{qualifier} targets {release.get('target_commitish')}, "
            f"expected {expected_commit}"
        )

    if published:
        if release.get("draft") or release.get("published_at") is None:
            raise CiError("release is still a draft")
        return

    assets = require_array(release.get("assets"), "release assets")
    asset_objects = [require_object(asset, "release asset") for asset in assets]
    expected_assets = expected_release_assets(expected_commit)
    actual_assets: set[str] = set()
    for asset in asset_objects:
        name = asset.get("name")
        if not isinstance(name, str):
            raise CiError(f"release asset has invalid name: {name!r}")
        actual_assets.add(name)
    if actual_assets != expected_assets or len(asset_objects) != len(expected_assets):
        raise CiError(
            "Expected 8 release distribution images. "
            "Release assets do not match the expected distribution set: "
            f"expected={sorted(expected_assets)!r}, actual={sorted(actual_assets)!r}"
        )

    invalid_assets = [
        asset
        for asset in asset_objects
        if not has_positive_size(asset) or asset.get("state") != "uploaded"
    ]
    if invalid_assets:
        raise CiError(f"release has incomplete assets: {invalid_assets!r}")


def prepare_release() -> None:
    repository = require_env("REPOSITORY")
    commit = require_env("GITHUB_SHA")
    api_root = f"{require_env('GITHUB_API_URL')}/repos/{repository}"
    release_tag = f"distro-{commit}"
    releases = github_json_request(
        "GET",
        f"{api_root}/releases?per_page=100",
        description="releases response",
    )
    release_id = find_draft_release_value(releases, release_tag, commit)
    if release_id is None:
        release = github_json_request(
            "POST",
            f"{api_root}/releases",
            payload={
                "tag_name": release_tag,
                "target_commitish": commit,
                "name": f"Nanvix Distribution {commit[:7]}",
                "body": RELEASE_NOTES.format(commit=commit),
                "draft": True,
                "prerelease": False,
            },
            description="created release response",
        )
        release_id = require_id(
            require_object(release, "created release response").get("id"),
            "release",
        )
    append_github_file("GITHUB_OUTPUT", "release-id", str(release_id))


def clean_submodule_build_artifacts() -> None:
    run(
        ("git", "submodule", "foreach", "--recursive", "git", "clean", "-ffdx"),
        cwd=workspace(),
    )


def create_short_drive_mapping() -> None:
    require_platform(windows=True)
    remove_drive_mapping()
    run(("subst", "N:", str(workspace())))
    makefile = Path("N:/nanvix/Makefile")
    if not makefile.is_file():
        raise CiError(f"Short drive mapping does not contain {makefile}")


def restore_directory_symlinks() -> None:
    require_platform(windows=True)
    nanvix = Path("N:/nanvix")
    entries = run_output(("git", "ls-files", "-s"), cwd=nanvix)
    for entry in entries.splitlines():
        match = re.match(r"^120000\s+\S+\s+\S+\t(.+)$", entry)
        if match is None:
            continue
        relative_path = match.group(1)
        full_path = nanvix / relative_path
        if not full_path.exists() or full_path.is_dir():
            continue
        target = full_path.read_text(encoding="utf-8").strip()
        if not target or len(target) > 500:
            continue
        resolved = (full_path.parent / target).resolve()
        if resolved.is_dir():
            full_path.unlink()
            run(("cmd", "/c", "mklink", "/J", str(full_path), str(resolved)))
            print(f"Junction: {relative_path} -> {target}")
        elif resolved.is_file():
            shutil.copy2(resolved, full_path)
            print(f"Copied: {relative_path} -> {target}")


def powershell_executable() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        raise CiError("PowerShell is not available")
    return executable


def remove_path(path: Path) -> None:
    if path.is_junction():
        path.rmdir()
    elif path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def isolate_cargo_home() -> None:
    require_platform(windows=True)
    cargo_target = Path(require_env("USERPROFILE")) / ".cargo/bin"
    isolated_cargo = Path(require_env("RUNNER_TEMP")) / ".cargo"
    isolated_cargo.mkdir(parents=True, exist_ok=True)
    cargo_bin = isolated_cargo / "bin"
    remove_path(cargo_bin)
    run(("cmd", "/c", "mklink", "/J", str(cargo_bin), str(cargo_target)))
    append_github_file("GITHUB_ENV", "CARGO_HOME", str(isolated_cargo))
    print(f"Isolated CARGO_HOME to {isolated_cargo}")


def install_rust_toolchain() -> None:
    require_platform(windows=True)
    nanvix = Path("N:/nanvix")
    with (nanvix / "rust-toolchain").open("rb") as toolchain_file:
        manifest = require_object(
            cast(object, tomllib.load(toolchain_file)),
            "rust-toolchain manifest",
        )
    toolchain = require_object(manifest.get("toolchain"), "toolchain configuration")
    channel = toolchain.get("channel")
    if not isinstance(channel, str):
        raise CiError("rust-toolchain does not define a channel")
    print(f"Expected toolchain: {channel}")
    run(("rustup", "toolchain", "install", channel), cwd=nanvix)
    run(
        ("rustup", "component", "add", "--toolchain", channel, "clippy", "rustfmt"),
        cwd=nanvix,
    )
    run(("rustup", "show"), cwd=nanvix)
    run(("cargo", "--version"), cwd=nanvix)


def setup_prerequisites() -> None:
    require_platform(windows=True)
    version = platform.win32_ver()[1]
    try:
        build_number = int(version.split(".")[-1])
    except ValueError as error:
        raise CiError(f"Cannot parse Windows version: {version}") from error
    if build_number >= 22000:
        print(
            f"Detected Windows build {build_number} "
            "(Windows 11 or later). Running z.ps1 setup..."
        )
        run(
            (powershell_executable(), "-NoProfile", "-File", "z.ps1", "setup"),
            cwd=Path("N:/nanvix"),
        )
    else:
        print(
            f"Detected Windows build {build_number} (< 22000). "
            "Skipping z.ps1 setup on CI runner."
        )


def relocate_cargo_state() -> None:
    require_platform(windows=False)
    home = Path.home()
    cargo_home = home / ".cache/nanvix-distro-cargo"
    cargo_target = home / ".cache/nanvix-distro-target"
    remove_path(cargo_home)
    remove_path(cargo_target)
    cargo_home.mkdir(parents=True)
    cargo_target.mkdir(parents=True)
    (cargo_home / "bin").symlink_to(home / ".cargo/bin", target_is_directory=True)
    nanvix_target = workspace() / "nanvix/target"
    remove_path(nanvix_target)
    nanvix_target.symlink_to(cargo_target, target_is_directory=True)
    append_github_file("GITHUB_ENV", "CARGO_HOME", str(cargo_home))
    append_github_file("GITHUB_ENV", "CI_NANVIX_TARGET_DIR", str(cargo_target))


def setup_kvm() -> None:
    require_platform(windows=False)
    rule = 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"\n'
    run(
        ("sudo", "tee", "/etc/udev/rules.d/99-kvm4all.rules"),
        input_text=rule,
    )
    run(("sudo", "udevadm", "control", "--reload-rules"))
    run(("sudo", "udevadm", "trigger", "--name-match=kvm"))
    kvm_device = "/dev/kvm"
    if not Path(kvm_device).exists():
        raise CiError("GitHub-hosted runner does not expose /dev/kvm")
    if not os.access(kvm_device, os.R_OK | os.W_OK):
        raise CiError("GitHub-hosted runner cannot read and write /dev/kvm")
    run(("ls", "-l", kvm_device))


def install_python_dependencies() -> None:
    command = [sys.executable, "-m", "pip", "install"]
    if not IS_WINDOWS:
        command.extend(("--user", "--break-system-packages"))
    command.extend(("black", "pyright", "tomli-w"))
    run(command)


def check_distro_tooling() -> None:
    root = workspace()
    run(
        (
            sys.executable,
            "-m",
            "black",
            "--target-version",
            "py312",
            "--check",
            "z.py",
            "nanvix_distro",
            "tests",
            "scripts",
        ),
        cwd=root,
    )
    run((sys.executable, "-m", "pyright"), cwd=root)
    run((sys.executable, "-m", "unittest", "discover", "-v"), cwd=root)


def command_succeeds(command: Sequence[str]) -> bool:
    return (
        subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def ensure_docker_daemon() -> None:
    require_platform(windows=False)
    if not command_succeeds(("docker", "info")):
        print("::warning::Docker daemon not responding; attempting to start it")
        if not command_succeeds(("sudo", "systemctl", "start", "docker")):
            command_succeeds(("sudo", "service", "docker", "start"))
        for attempt in range(5):
            if command_succeeds(("docker", "info")):
                break
            if attempt < 4:
                time.sleep(2)
    run(("docker", "info"))


def run_z(*arguments: str) -> None:
    run((sys.executable, "z.py", "--verbose", *arguments), cwd=workspace())


def build() -> None:
    require_platform(windows=False)
    run_z("build")


def build_test_nanvix_core() -> None:
    require_platform(windows=True)
    nanvix = Path("N:/nanvix")
    environment = os.environ.copy()
    environment["HOME"] = require_env("USERPROFILE").replace("\\", "/")
    git = shutil.which("git")
    if git is None:
        raise CiError("git is not available")
    git_usr_bin = Path(git).parent.parent / "usr/bin"
    if git_usr_bin.is_dir():
        environment["PATH"] = f"{environment['PATH']};{git_usr_bin}"

    make_arguments = [
        "DEPLOYMENT_MODE=standalone",
        "WHP=yes",
        "SYSROOT_DIR=N:/build/sysroot",
    ]
    release_flag = os.environ.get("RELEASE_FLAG", "")
    if release_flag:
        make_arguments.append(release_flag)
    test_arguments = [
        *make_arguments,
        "all",
        "test",
        f"MACHINE={require_env('MACHINE_TYPE')}",
        f"LOG_LEVEL={require_env('TEST_LOG_LEVEL')}",
    ]
    run(("make", *test_arguments), cwd=nanvix, env=environment)
    install_arguments = [
        *make_arguments,
        "install",
        f"MACHINE={require_env('MACHINE_TYPE')}",
        f"LOG_LEVEL={require_env('LOG_LEVEL')}",
    ]
    run(("make", *install_arguments), cwd=nanvix, env=environment)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_zip(archive: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive) as zip_file:
        for member in zip_file.infolist():
            member_path = (destination / member.filename).resolve()
            if not member_path.is_relative_to(destination_root):
                raise CiError(f"Archive contains an unsafe path: {member.filename}")
        zip_file.extractall(destination)


def download_pinned_distribution_guests() -> None:
    require_platform(windows=True)
    root = workspace()
    runner_temp = Path(require_env("RUNNER_TEMP"))
    for package in GUEST_PACKAGES:
        repository = root / "usr/bin" / package.name
        run(("git", "fetch", "--tags", "--force", "origin"), cwd=repository)
        tag = run_output(
            ("git", "describe", "--tags", "--exact-match"),
            cwd=repository,
        )
        release = require_object(
            github_json_request(
                "GET",
                f"https://api.github.com/repos/nanvix/{package.name}/releases/tags/{tag}",
                description=f"{package.name} release response",
            ),
            f"{package.name} release response",
        )
        assets = require_array(release.get("assets"), f"{package.name} release assets")
        matches = [
            require_object(asset, f"{package.name} release asset")
            for asset in assets
            if require_object(asset, f"{package.name} release asset").get("name")
            == package.asset
        ]
        if len(matches) != 1:
            raise CiError(
                f"Pinned {package.name} release does not publish exactly one "
                f"{package.asset}"
            )
        metadata = matches[0]
        expected_digest = metadata.get("digest")
        if not isinstance(expected_digest, str) or not expected_digest.startswith(
            "sha256:"
        ):
            raise CiError(
                f"Pinned {package.name} release does not publish a SHA-256 "
                f"digest for {package.asset}"
            )
        download_url = metadata.get("browser_download_url")
        if not isinstance(download_url, str):
            raise CiError(f"Pinned {package.name} release has no download URL")

        archive = runner_temp / package.asset
        destination = root / "build/deps" / package.name
        remove_path(destination)
        destination.mkdir(parents=True)
        download_file(download_url, archive)
        if sha256(archive).lower() != expected_digest.removeprefix("sha256:").lower():
            raise CiError(f"Pinned {package.name} archive SHA-256 digest mismatch")
        extract_zip(archive, destination)
        for required_file in package.required_files:
            required_path = destination / required_file
            if not required_path.is_file():
                raise CiError(
                    f"Pinned {package.name} archive does not contain {required_file}"
                )
            print(f"{required_path} ({required_path.stat().st_size} bytes)")


def create_distribution_images() -> None:
    for profile in ("busybox", "javascript", "python"):
        run_z("dist", profile)
    run_z("menuconfig", "ci-composed", "--include", "all")


def terminate_process(process: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        run(
            ("taskkill", "/PID", str(process.pid), "/T", "/F"),
            check=False,
        )
    else:
        os.killpg(process.pid, signal.SIGKILL)


def smoke_test(
    profile: str,
    standard_input: str,
    markers: Sequence[str],
    timeout_seconds: int,
) -> None:
    root = workspace()
    log_name = (
        "composed-smoke.log" if profile == "ci-composed" else f"{profile}-smoke.log"
    )
    log = root / "build/dist" / profile / log_name
    command = (sys.executable, "z.py", "--verbose", "run", profile)
    print(f"[INFO] {shlex.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=not IS_WINDOWS,
    )
    try:
        output, _ = process.communicate(
            input=standard_input,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        terminate_process(process)
        output, _ = process.communicate()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(output, encoding="utf-8")
        host = "WHP" if IS_WINDOWS else "KVM"
        raise CiError(f"{host} smoke test timed out after {timeout_seconds} seconds")

    print(output, end="")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(output, encoding="utf-8")
    if process.returncode != 0:
        raise CiError(f"Smoke test failed with exit code {process.returncode}")
    for marker in markers:
        if marker not in output:
            raise CiError(f"Smoke marker not found: {marker}")


def smoke_test_busybox() -> None:
    smoke_test("busybox", "exit\n", ("NANVIX_BUSYBOX_READY",), 120)


def smoke_test_composed() -> None:
    require_platform(windows=False)
    smoke_test(
        "ci-composed",
        "\n".join(
            (
                "python3 -c 'print(\"PYTHON_COMPONENT_READY\")'",
                "qjs -e 'console.log(\"JAVASCRIPT_COMPONENT_READY\")'",
                "exit",
                "",
            )
        ),
        ("PYTHON_COMPONENT_READY", "JAVASCRIPT_COMPONENT_READY"),
        180,
    )


def distribution_files(source: Path) -> tuple[Path, ...]:
    executable = "nanvixd.exe" if IS_WINDOWS else "nanvixd.elf"
    relative_paths = (
        Path(executable),
        Path("bin/kernel.elf"),
        Path("bin/nanvix.initrd"),
        Path("bin/nanvix.ramfs"),
    )
    for relative_path in relative_paths:
        if not (source / relative_path).is_file():
            raise CiError(f"Distribution artifact not found: {source / relative_path}")
    return relative_paths


def create_tar_archive(
    archive: Path,
    source: Path,
    relative_paths: Sequence[Path],
    commit_timestamp: int,
) -> None:
    with archive.open("wb") as archive_file:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=archive_file,
            mtime=commit_timestamp,
        ) as gzip_file:
            with tarfile.open(
                fileobj=gzip_file,
                mode="w",
                format=tarfile.GNU_FORMAT,
            ) as tar_file:
                for relative_path in sorted(relative_paths):
                    artifact = source / relative_path
                    info = tar_file.gettarinfo(str(artifact), relative_path.as_posix())
                    info.mtime = commit_timestamp
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    with artifact.open("rb") as artifact_file:
                        tar_file.addfile(info, artifact_file)


def create_zip_archive(
    archive: Path,
    source: Path,
    relative_paths: Sequence[Path],
    commit_timestamp: int,
) -> None:
    timestamp = datetime.fromtimestamp(commit_timestamp, timezone.utc)
    if timestamp.year < 1980:
        raise CiError("Commit timestamp predates the ZIP file format")
    zip_timestamp = (
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        timestamp.second,
    )
    with zipfile.ZipFile(
        archive,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as zip_file:
        for relative_path in sorted(relative_paths):
            artifact = source / relative_path
            info = zipfile.ZipInfo(relative_path.as_posix(), zip_timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = stat.S_IMODE(artifact.stat().st_mode) << 16
            with artifact.open("rb") as artifact_file:
                with zip_file.open(info, mode="w") as archive_file:
                    shutil.copyfileobj(artifact_file, archive_file)


def package_release_distributions() -> None:
    root = workspace()
    commit = require_env("GITHUB_SHA")
    timestamp_text = run_output(
        ("git", "show", "-s", "--format=%ct", commit),
        cwd=root,
    )
    try:
        commit_timestamp = int(timestamp_text)
    except ValueError as error:
        raise CiError(f"Cannot resolve commit timestamp for {commit}") from error

    release_directory = root / "release-distributions"
    release_directory.mkdir(parents=True, exist_ok=True)
    host = "windows" if IS_WINDOWS else "linux"
    extension = ".zip" if IS_WINDOWS else ".tar.gz"
    for profile, components in PROFILES:
        source = root / "build/dist" / profile
        if not source.is_dir():
            raise CiError(f"Distribution output not found: {source}")
        archive = release_directory / (
            f"nanvix-distro-{host}-x86-microvm-256mb-"
            f"{components}-{commit}{extension}"
        )
        archive.unlink(missing_ok=True)
        relative_paths = distribution_files(source)
        if IS_WINDOWS:
            create_zip_archive(archive, source, relative_paths, commit_timestamp)
        else:
            create_tar_archive(archive, source, relative_paths, commit_timestamp)
        if archive.stat().st_size <= 0:
            raise CiError(f"Distribution archive is empty: {archive}")


def reclaim_build_space() -> None:
    require_platform(windows=False)
    root = workspace()
    logs = root / "ci-logs"
    logs.mkdir(exist_ok=True)
    shutil.copy2(root / "build/dist/busybox/busybox-smoke.log", logs)
    shutil.copy2(root / "build/dist/ci-composed/composed-smoke.log", logs)
    shutil.rmtree(root / "build")
    clean_submodule_build_artifacts()
    target = root / "nanvix/target"
    remove_path(target)
    target.symlink_to(
        Path(require_env("CI_NANVIX_TARGET_DIR")),
        target_is_directory=True,
    )
    run(("df", "-h", str(root), str(Path.home())))


def run_tests() -> None:
    require_platform(windows=False)
    run_z("test")


def stage_release_distributions() -> None:
    release_id = require_release_id(require_env("RELEASE_ID"))
    repository = require_env("REPOSITORY")
    api_root = f"{require_env('GITHUB_API_URL')}/repos/{repository}"
    extension = "*.zip" if IS_WINDOWS else "*.tar.gz"
    content_type = "application/zip" if IS_WINDOWS else "application/gzip"
    assets = sorted((workspace() / "release-distributions").glob(extension))
    if len(assets) != 4:
        host = "Windows " if IS_WINDOWS else ""
        raise CiError(
            f"Expected 4 {host}release distribution images, found {len(assets)}"
        )

    release_assets = github_json_request(
        "GET",
        f"{api_root}/releases/{release_id}/assets?per_page=100",
        description="release assets response",
    )
    for asset in assets:
        for asset_id in find_release_asset_ids_value(release_assets, asset.name):
            github_request("DELETE", f"{api_root}/releases/assets/{asset_id}")
        upload_url = (
            f"https://uploads.github.com/repos/{repository}/releases/"
            f"{release_id}/assets?name={quote(asset.name, safe='')}"
        )
        uploaded = decode_json(
            github_request(
                "POST",
                upload_url,
                payload=asset,
                content_type=content_type,
            ),
            "uploaded asset response",
        )
        validate_uploaded_asset_value(uploaded, asset.name)


def remove_relocated_cargo_state() -> None:
    require_platform(windows=False)
    run(("df", "-h", str(workspace()), str(Path.home())))
    target = workspace() / "nanvix/target"
    if target.is_symlink():
        target.unlink()
    remove_path(Path.home() / ".cache/nanvix-distro-cargo")
    remove_path(Path.home() / ".cache/nanvix-distro-target")


def print_sccache_statistics() -> None:
    if shutil.which("sccache") is not None:
        run(("sccache", "--show-stats"))


def publish_release() -> None:
    release_id = require_release_id(require_env("RELEASE_ID"))
    repository = require_env("REPOSITORY")
    commit = require_env("GITHUB_SHA")
    api_root = f"{require_env('GITHUB_API_URL')}/repos/{repository}"
    release_tag = f"distro-{commit}"
    release_url = f"{api_root}/releases/{release_id}"
    release = github_json_request(
        "GET",
        release_url,
        description="release response",
    )
    validate_release_value(release, release_tag, commit, published=False)
    published = github_json_request(
        "PATCH",
        release_url,
        payload={
            "tag_name": release_tag,
            "target_commitish": commit,
            "draft": False,
            "make_latest": "true",
        },
        description="published release response",
    )
    validate_release_value(published, release_tag, commit, published=True)


def remove_drive_mapping() -> None:
    require_platform(windows=True)
    if Path("N:/").exists():
        run(("subst", "N:", "/D"))


TASKS: dict[str, Callable[[], None]] = {
    "prepare-release": prepare_release,
    "clean-submodule-build-artifacts": clean_submodule_build_artifacts,
    "create-short-drive-mapping": create_short_drive_mapping,
    "restore-directory-symlinks": restore_directory_symlinks,
    "isolate-cargo-home": isolate_cargo_home,
    "install-rust-toolchain": install_rust_toolchain,
    "setup-prerequisites": setup_prerequisites,
    "relocate-cargo-state": relocate_cargo_state,
    "setup-kvm": setup_kvm,
    "install-python-dependencies": install_python_dependencies,
    "check-distro-tooling": check_distro_tooling,
    "ensure-docker-daemon": ensure_docker_daemon,
    "build": build,
    "build-test-nanvix-core": build_test_nanvix_core,
    "download-pinned-distribution-guests": download_pinned_distribution_guests,
    "create-distribution-images": create_distribution_images,
    "smoke-test-busybox": smoke_test_busybox,
    "smoke-test-composed": smoke_test_composed,
    "package-release-distributions": package_release_distributions,
    "reclaim-build-space": reclaim_build_space,
    "test": run_tests,
    "stage-release-distributions": stage_release_distributions,
    "remove-relocated-cargo-state": remove_relocated_cargo_state,
    "print-sccache-statistics": print_sccache_statistics,
    "publish-release": publish_release,
    "remove-drive-mapping": remove_drive_mapping,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for task in TASKS:
        subparsers.add_parser(task)

    find_draft_parser = subparsers.add_parser("find-draft-release")
    find_draft_parser.add_argument("releases")
    find_draft_parser.add_argument("expected_tag")
    find_draft_parser.add_argument("expected_commit")

    find_assets_parser = subparsers.add_parser("find-release-asset-ids")
    find_assets_parser.add_argument("assets")
    find_assets_parser.add_argument("expected_name")

    validate_asset_parser = subparsers.add_parser("validate-uploaded-asset")
    validate_asset_parser.add_argument("asset")
    validate_asset_parser.add_argument("expected_name")

    for command in ("validate-release", "validate-published-release"):
        validate_release_parser = subparsers.add_parser(command)
        validate_release_parser.add_argument("release")
        validate_release_parser.add_argument("expected_tag")
        validate_release_parser.add_argument("expected_commit")
    return parser.parse_args()


def run_command(args: argparse.Namespace) -> None:
    command = cast(str, args.command)
    if command in TASKS:
        TASKS[command]()
    elif command == "find-draft-release":
        release_id = find_draft_release_value(
            load_json(cast(str, args.releases)),
            cast(str, args.expected_tag),
            cast(str, args.expected_commit),
        )
        if release_id is not None:
            print(release_id)
    elif command == "find-release-asset-ids":
        for asset_id in find_release_asset_ids_value(
            load_json(cast(str, args.assets)),
            cast(str, args.expected_name),
        ):
            print(asset_id)
    elif command == "validate-uploaded-asset":
        validate_uploaded_asset_value(
            load_json(cast(str, args.asset)),
            cast(str, args.expected_name),
        )
    elif command in ("validate-release", "validate-published-release"):
        validate_release_value(
            load_json(cast(str, args.release)),
            cast(str, args.expected_tag),
            cast(str, args.expected_commit),
            published=command == "validate-published-release",
        )


def fail(message: str) -> NoReturn:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    try:
        run_command(parse_args())
    except CiError as error:
        fail(str(error))
    except subprocess.CalledProcessError as error:
        fail(f"Command failed with exit code {error.returncode}: {error.cmd}")
    except OSError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
