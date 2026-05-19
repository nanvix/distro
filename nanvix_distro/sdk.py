"""Nanvix SDK release-contract loading and validation."""

from __future__ import annotations

import json
import os
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_FETCH_RETRY_DELAYS = (5.0, 10.0, 20.0, 30.0, 30.0)
_RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class ContractError(ValueError):
    """Raised when an SDK contract or consumer pin is invalid."""


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")

    result: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise ContractError(f"{field} contains a non-string key")
        result[key] = item
    return result


def _required_mapping(
    data: dict[str, object], key: str, field: str
) -> dict[str, object]:
    if key not in data:
        raise ContractError(f"{field}.{key} is required")
    return _mapping(data[key], f"{field}.{key}")


def _required_string(data: dict[str, object], key: str, field: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ContractError(f"{field}.{key} must be a non-empty string")
    return value


def _required_integer(data: dict[str, object], key: str, field: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(f"{field}.{key} must be an integer")
    return value


def _load_toml(path: Path) -> dict[str, object]:
    try:
        parsed = cast(object, tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"cannot read {path}: {exc}") from exc
    return _mapping(parsed, str(path))


@dataclass(frozen=True)
class SDKContract:
    """The immutable coordinates published in an SDK release contract."""

    schema_version: int
    sdk_version: str
    image_name: str
    image_digest: str
    image_ref: str
    nanvix_tag: str
    nanvix_version: str
    nanvix_commit: str
    sysroot_sha256: str
    c_abi: str
    target_triple: str

    @classmethod
    def load(cls, path: Path) -> SDKContract:
        """Load and validate an SDK release contract from JSON."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContractError(f"cannot read {path}: {exc}") from exc
        return cls.from_json(text, source=str(path))

    @classmethod
    def from_json(cls, text: str, *, source: str) -> SDKContract:
        """Parse and validate an SDK release contract JSON document."""
        try:
            parsed = cast(object, json.loads(text))
        except json.JSONDecodeError as exc:
            raise ContractError(f"cannot parse {source}: {exc}") from exc
        root = _mapping(parsed, source)
        image = _required_mapping(root, "image", source)
        libc = _required_mapping(root, "libc", source)
        compat = _required_mapping(root, "compat", source)
        target = _required_mapping(root, "target", source)

        contract = cls(
            schema_version=_required_integer(root, "schema_version", source),
            sdk_version=_required_string(root, "sdk_version", source),
            image_name=_required_string(image, "name", "image"),
            image_digest=_required_string(image, "digest", "image"),
            image_ref=_required_string(image, "ref", "image"),
            nanvix_tag=_required_string(libc, "nanvix_tag", "libc"),
            nanvix_version=_required_string(libc, "nanvix_version", "libc"),
            nanvix_commit=_required_string(libc, "nanvix_commit", "libc"),
            sysroot_sha256=_required_string(libc, "sysroot_sha256", "libc"),
            c_abi=_required_string(compat, "c_abi", "compat"),
            target_triple=_required_string(target, "triple", "target"),
        )
        contract._validate()
        return contract

    @property
    def release_coordinate(self) -> str:
        """Return the SDK suffix used by SDK-qualified port releases."""
        prefix = f"v{self.nanvix_version}-"
        if not self.sdk_version.startswith(prefix):
            raise ContractError(
                f"SDK version {self.sdk_version!r} does not target Nanvix "
                f"{self.nanvix_version!r}"
            )
        return self.sdk_version.removeprefix("v")

    def qualified_release_tag(self, package_version: str) -> str:
        """Return the release tag for a package in this SDK release set."""
        if not package_version:
            raise ContractError("package version must not be empty")
        return f"{package_version}-nanvix-{self.release_coordinate}"

    def validate_port(self, port_dir: Path) -> None:
        """Validate a port manifest and lockfile against this contract."""
        manifest_path = port_dir / ".nanvix" / "nanvix.toml"
        lock_path = port_dir / ".nanvix" / "nanvix.lock"
        self._validate_port_documents(
            _load_toml(manifest_path),
            _load_toml(lock_path),
            source=str(port_dir),
        )

    def validate_port_text(self, manifest: str, lock: str, *, source: str) -> None:
        """Validate in-memory port manifest and lockfile documents."""
        try:
            manifest_data = _mapping(
                cast(object, tomllib.loads(manifest)),
                f"{source}/.nanvix/nanvix.toml",
            )
            lock_data = _mapping(
                cast(object, tomllib.loads(lock)),
                f"{source}/.nanvix/nanvix.lock",
            )
        except tomllib.TOMLDecodeError as exc:
            raise ContractError(
                f"cannot parse SDK metadata for {source}: {exc}"
            ) from exc
        self._validate_port_documents(manifest_data, lock_data, source=source)

    def _validate_port_documents(
        self,
        manifest: dict[str, object],
        lock: dict[str, object],
        *,
        source: str,
    ) -> None:
        """Validate parsed port metadata against this contract."""

        package = _required_mapping(manifest, "package", source)
        toolchain = _required_mapping(manifest, "toolchain", source)
        metadata = _required_mapping(lock, "metadata", source)
        sdk = _required_mapping(metadata, "sdk", "metadata")

        expected_manifest = {
            "nanvix-version": self.nanvix_version,
            "sdk-version": self.sdk_version,
            "sdk-image": self.image_name,
            "sdk-digest": self.image_digest,
        }
        actual_manifest = {
            "nanvix-version": _required_string(package, "nanvix-version", "package"),
            "sdk-version": _required_string(toolchain, "sdk-version", "toolchain"),
            "sdk-image": _required_string(toolchain, "sdk-image", "toolchain"),
            "sdk-digest": _required_string(toolchain, "sdk-digest", "toolchain"),
        }
        if actual_manifest != expected_manifest:
            raise ContractError(
                f"{source}: manifest SDK coordinate differs from "
                f"{self.sdk_version} ({self.image_digest})"
            )

        expected_lock = {
            "sdk-version": self.sdk_version,
            "image-name": self.image_name,
            "image-digest": self.image_digest,
            "image-ref": self.image_ref,
            "nanvix-tag": self.nanvix_tag,
            "nanvix-version": self.nanvix_version,
            "nanvix-commit": self.nanvix_commit,
            "sysroot-sha256": self.sysroot_sha256,
        }
        actual_lock = {
            key: _required_string(sdk, key, "metadata.sdk") for key in expected_lock
        }
        if actual_lock != expected_lock:
            raise ContractError(
                f"{source}: lockfile SDK coordinate differs from "
                f"{self.sdk_version} ({self.image_digest})"
            )

    def _validate(self) -> None:
        if self.schema_version != 1:
            raise ContractError(
                f"unsupported SDK contract schema {self.schema_version}"
            )
        if not self.sdk_version.startswith("v"):
            raise ContractError("sdk_version must start with 'v'")
        if not self.nanvix_tag.startswith("v"):
            raise ContractError("libc.nanvix_tag must start with 'v'")
        if self.nanvix_tag.removeprefix("v") != self.nanvix_version:
            raise ContractError("libc Nanvix tag and version disagree")
        if len(self.nanvix_commit) != 40:
            raise ContractError("libc.nanvix_commit must be a full Git commit")
        if not self.image_digest.startswith("sha256:"):
            raise ContractError("image.digest must be a sha256 digest")
        if self.image_ref != f"{self.image_name}@{self.image_digest}":
            raise ContractError("image.ref must combine image.name and image.digest")
        if not self.sysroot_sha256 or len(self.sysroot_sha256) != 64:
            raise ContractError("libc.sysroot_sha256 must be a SHA-256 checksum")
        _ = self.release_coordinate


@dataclass(frozen=True)
class FetchedSDKContract:
    """A validated SDK contract and its complete JSON document."""

    contract: SDKContract
    document: dict[str, object]
    release_tag: str

    def write(self, path: Path) -> None:
        """Atomically write the canonical contract document."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(self.document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)


def _fetch_json(url: str, *, token: str | None) -> tuple[dict[str, object], str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "nanvix-distro",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    text: str | None = None
    for attempt in range(len(_FETCH_RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP_STATUS or attempt == len(
                _FETCH_RETRY_DELAYS
            ):
                raise ContractError(f"cannot fetch {url}: {exc}") from exc
        except (OSError, urllib.error.URLError) as exc:
            if attempt == len(_FETCH_RETRY_DELAYS):
                raise ContractError(f"cannot fetch {url}: {exc}") from exc
        except UnicodeError as exc:
            raise ContractError(f"cannot fetch {url}: {exc}") from exc

        time.sleep(_FETCH_RETRY_DELAYS[attempt])

    if text is None:
        raise ContractError(f"cannot fetch {url}: retry loop ended unexpectedly")

    try:
        document = _mapping(cast(object, json.loads(text)), url)
    except json.JSONDecodeError as exc:
        raise ContractError(f"cannot parse {url}: {exc}") from exc
    return document, text


def fetch_sdk_contract(
    sdk_version: str | None = None,
    *,
    token: str | None = None,
) -> FetchedSDKContract:
    """Fetch a completed SDK release contract from GitHub Releases."""
    if sdk_version:
        encoded = urllib.parse.quote(sdk_version, safe="")
        release_url = f"https://api.github.com/repos/nanvix/sdk/releases/tags/{encoded}"
    else:
        release_url = "https://api.github.com/repos/nanvix/sdk/releases/latest"

    release, _ = _fetch_json(release_url, token=token)
    release_tag = _required_string(release, "tag_name", release_url)
    assets_value = release.get("assets")
    if not isinstance(assets_value, list):
        raise ContractError(f"{release_url}.assets must be an array")

    contract_url: str | None = None
    for index, value in enumerate(cast(list[object], assets_value)):
        asset = _mapping(value, f"{release_url}.assets[{index}]")
        if asset.get("name") == "sdk-release.json":
            contract_url = _required_string(
                asset,
                "browser_download_url",
                f"{release_url}.assets[{index}]",
            )
            break
    if contract_url is None:
        raise ContractError(f"{release_tag} is incomplete: sdk-release.json is missing")

    document, text = _fetch_json(contract_url, token=token)
    contract = SDKContract.from_json(text, source=contract_url)
    if contract.sdk_version != release_tag:
        raise ContractError(
            f"SDK release tag {release_tag!r} disagrees with contract "
            f"{contract.sdk_version!r}"
        )
    return FetchedSDKContract(
        contract=contract,
        document=document,
        release_tag=release_tag,
    )
