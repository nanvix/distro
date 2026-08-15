#!/usr/bin/env python3

# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn, Sequence, cast


class CiError(RuntimeError):
    """Report a CI failure without a Python traceback."""


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CiError(f"Required environment variable is not set: {name}")
    return value


def append_github_output(name: str, value: str) -> None:
    with Path(require_env("GITHUB_OUTPUT")).open("a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def run(
    command: Sequence[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True)


def run_output(command: Sequence[str]) -> str:
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def upgrade() -> None:
    event_name = require_env("EVENT_NAME")
    sdk_version = os.environ.get("SDK_VERSION", "")
    if event_name == "repository_dispatch" and not sdk_version:
        raise CiError("Distro release candidate did not specify an SDK version")
    if sdk_version and not re.fullmatch(r"v\d+\.\d+\.\d+-sdk\.\d+", sdk_version):
        raise CiError(f"Invalid distro release candidate SDK: {sdk_version}")

    command = [
        sys.executable,
        "z.py",
        "--verbose",
        "upgrade",
        "--defer-incomplete-release-set",
    ]
    if sdk_version:
        command.extend(("--sdk-version", sdk_version))
    run(command)

    diff = run(("git", "diff", "--quiet", "HEAD", "--"), check=False)
    if diff.returncode == 0:
        append_github_output("changed", "false")
        print("::notice::No coherent newer distro release set is available yet")
        return
    if diff.returncode != 1:
        raise CiError(f"git diff failed with exit code {diff.returncode}")

    append_github_output("changed", "true")
    with Path("config/sdk-release.json").open(encoding="utf-8") as release_file:
        release = cast(object, json.load(release_file))
    if not isinstance(release, dict):
        raise CiError("config/sdk-release.json has no valid sdk_version")
    release_object = cast(dict[str, object], release)
    sdk_version_value = release_object.get("sdk_version")
    if not isinstance(sdk_version_value, str):
        raise CiError("config/sdk-release.json has no valid sdk_version")
    append_github_output("sdk_version", sdk_version_value)


def install_python_dependencies() -> None:
    run((sys.executable, "-m", "pip", "install", "./usr/lib/zutils"))


def test_upgraded_release_set() -> None:
    run((sys.executable, "-m", "unittest", "discover", "-v"))


def list_ci_run_ids(repository: str, head_sha: str) -> list[str]:
    output = run_output(
        (
            "gh",
            "run",
            "list",
            "--repo",
            repository,
            "--workflow",
            "self-hosted-test.yml",
            "--event",
            "workflow_dispatch",
            "--commit",
            head_sha,
            "--limit",
            "100",
            "--json",
            "databaseId",
            "--jq",
            ".[].databaseId",
        )
    )
    return output.splitlines()


def wait_for_ci() -> None:
    repository = require_env("REPOSITORY")
    pull_request = require_env("PR_NUMBER")
    head_sha = run_output(
        (
            "gh",
            "pr",
            "view",
            pull_request,
            "--repo",
            repository,
            "--json",
            "headRefOid",
            "--jq",
            ".headRefOid",
        )
    )
    head_ref = run_output(
        (
            "gh",
            "pr",
            "view",
            pull_request,
            "--repo",
            repository,
            "--json",
            "headRefName",
            "--jq",
            ".headRefName",
        )
    )
    existing_run_ids = set(list_ci_run_ids(repository, head_sha))
    run(
        (
            "gh",
            "workflow",
            "run",
            "self-hosted-test.yml",
            "--repo",
            repository,
            "--ref",
            head_ref,
        )
    )

    run_id = ""
    for _ in range(30):
        new_run_ids = [
            candidate
            for candidate in list_ci_run_ids(repository, head_sha)
            if candidate not in existing_run_ids
        ]
        if new_run_ids:
            run_id = new_run_ids[0]
            break
        time.sleep(10)
    if not run_id:
        raise CiError(f"Distro Test did not start for commit {head_sha}")

    status = ""
    conclusion = ""
    for _ in range(360):
        state = subprocess.run(
            (
                "gh",
                "api",
                f"repos/{repository}/actions/runs/{run_id}",
                "--jq",
                '[.status, (.conclusion // "")] | @tsv',
            ),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if state.returncode == 0:
            status, conclusion = state.stdout.rstrip("\n").split("\t", maxsplit=1)
            if status == "completed":
                break
        else:
            detail = state.stdout.strip()
            print(f"::warning::Could not query CI run {run_id}; retrying: {detail}")
        time.sleep(30)

    if status != "completed":
        raise CiError(f"Timed out waiting for CI run {run_id}")
    if conclusion != "success":
        raise CiError(f"CI run {run_id} concluded {conclusion}")
    append_github_output("head-sha", head_sha)


def merge_pull_request() -> None:
    repository = require_env("REPOSITORY")
    pull_request = require_env("PR_NUMBER")
    run(
        (
            "gh",
            "pr",
            "merge",
            "--repo",
            repository,
            "--merge",
            "--match-head-commit",
            require_env("MERGE_HEAD_SHA"),
            pull_request,
        )
    )
    merge_commit = run_output(
        (
            "gh",
            "pr",
            "view",
            pull_request,
            "--repo",
            repository,
            "--json",
            "mergeCommit",
            "--jq",
            ".mergeCommit.oid // empty",
        )
    )
    if not re.fullmatch(r"[0-9a-f]{40}", merge_commit):
        raise CiError(f"Pull request has no valid merge commit: {merge_commit!r}")
    run(
        (
            "gh",
            "workflow",
            "run",
            "self-hosted-test.yml",
            "--repo",
            repository,
            "--ref",
            "main",
            "--field",
            "release=true",
            "--field",
            f"expected_commit={merge_commit}",
        )
    )


def fail(message: str) -> NoReturn:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "task",
        choices=(
            "upgrade",
            "install-python-dependencies",
            "test-upgraded-release-set",
            "wait-for-ci",
            "merge-pull-request",
        ),
    )
    task = parser.parse_args().task
    tasks = {
        "upgrade": upgrade,
        "install-python-dependencies": install_python_dependencies,
        "test-upgraded-release-set": test_upgraded_release_set,
        "wait-for-ci": wait_for_ci,
        "merge-pull-request": merge_pull_request,
    }
    try:
        tasks[task]()
    except CiError as error:
        fail(str(error))
    except subprocess.CalledProcessError as error:
        fail(f"Command failed with exit code {error.returncode}: {error.cmd}")


if __name__ == "__main__":
    main()
