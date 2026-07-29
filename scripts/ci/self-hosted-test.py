#!/usr/bin/env python3

# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

import argparse
import json
from pathlib import Path
from typing import cast


def load_json(path: str) -> object:
    with Path(path).open(encoding="utf-8") as json_file:
        return cast(object, json.load(json_file))


def require_object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SystemExit(f"{description} is not a JSON object")
    return cast(dict[str, object], value)


def require_array(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise SystemExit(f"{description} is not a JSON array")
    return cast(list[object], value)


def require_id(value: object, description: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SystemExit(f"{description} has invalid id: {value!r}")
    return value


def has_positive_size(asset: dict[str, object]) -> bool:
    size = asset.get("size")
    return isinstance(size, int) and not isinstance(size, bool) and size > 0


def find_draft_release(args: argparse.Namespace) -> None:
    releases = require_array(load_json(args.releases), "releases response")
    matches: list[dict[str, object]] = []
    for value in releases:
        release = require_object(value, "release")
        if release.get("tag_name") == args.expected_tag:
            matches.append(release)

    if len(matches) > 1:
        raise SystemExit(
            f"expected at most one release tagged {args.expected_tag!r}, "
            f"found {len(matches)}"
        )
    if not matches:
        return

    release = matches[0]
    if release.get("target_commitish") != args.expected_commit:
        raise SystemExit(
            f"release targets {release.get('target_commitish')}, "
            f"expected {args.expected_commit}"
        )
    if not release.get("draft"):
        raise SystemExit(f"release {release.get('id')} is already published")

    print(require_id(release.get("id"), "release"))


def find_release_asset_ids(args: argparse.Namespace) -> None:
    assets = require_array(load_json(args.assets), "release assets response")
    for value in assets:
        asset = require_object(value, "release asset")
        if asset.get("name") == args.expected_name:
            print(require_id(asset.get("id"), "release asset"))


def validate_uploaded_asset(args: argparse.Namespace) -> None:
    asset = require_object(load_json(args.asset), "uploaded asset response")
    if asset.get("name") != args.expected_name:
        raise SystemExit(f"uploaded asset has unexpected name: {asset.get('name')!r}")
    if asset.get("state") != "uploaded" or not has_positive_size(asset):
        raise SystemExit(f"uploaded asset is incomplete: {asset!r}")


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


def validate_release(args: argparse.Namespace, *, published: bool) -> None:
    release = require_object(load_json(args.release), "release response")
    if release.get("tag_name") != args.expected_tag:
        qualifier = "published release" if published else "release"
        raise SystemExit(f"{qualifier} has unexpected tag: {release.get('tag_name')!r}")
    if release.get("target_commitish") != args.expected_commit:
        qualifier = "published release" if published else "release"
        raise SystemExit(
            f"{qualifier} targets {release.get('target_commitish')}, "
            f"expected {args.expected_commit}"
        )

    if published:
        if release.get("draft") or release.get("published_at") is None:
            raise SystemExit("release is still a draft")
        return

    assets = require_array(release.get("assets"), "release assets")
    asset_objects = [require_object(asset, "release asset") for asset in assets]
    expected_assets = expected_release_assets(args.expected_commit)
    actual_assets: set[str] = set()
    for asset in asset_objects:
        name = asset.get("name")
        if not isinstance(name, str):
            raise SystemExit(f"release asset has invalid name: {name!r}")
        actual_assets.add(name)
    if actual_assets != expected_assets or len(asset_objects) != len(expected_assets):
        raise SystemExit(
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
        raise SystemExit(f"release has incomplete assets: {invalid_assets!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

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


def main() -> None:
    args = parse_args()
    if args.command == "find-draft-release":
        find_draft_release(args)
    elif args.command == "find-release-asset-ids":
        find_release_asset_ids(args)
    elif args.command == "validate-uploaded-asset":
        validate_uploaded_asset(args)
    elif args.command == "validate-release":
        validate_release(args, published=False)
    elif args.command == "validate-published-release":
        validate_release(args, published=True)


if __name__ == "__main__":
    main()
