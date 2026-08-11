"""Cross-platform CI task runner tests."""

from __future__ import annotations

import importlib.util
import os
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import call, patch


class SelfHostedCiTests(unittest.TestCase):
    """Verify behavior shared by Linux and Windows CI jobs."""

    ROOT = Path(__file__).parents[1]

    @classmethod
    def load_runner(cls) -> types.ModuleType:
        module_name = "_nanvix_self_hosted_test"
        spec = importlib.util.spec_from_file_location(
            module_name,
            cls.ROOT / "scripts/ci/self-hosted-test.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {module_name: module}):
            spec.loader.exec_module(module)
        return module

    def test_release_packaging_is_shared_and_deterministic(self) -> None:
        """Both host formats contain the same four normalized runtime files."""
        runner = self.load_runner()
        package_distributions = cast(
            Callable[[], None],
            getattr(runner, "package_release_distributions"),
        )
        required_files = (
            "bin/kernel.elf",
            "bin/nanvix.initrd",
            "bin/nanvix.ramfs",
        )

        for windows, executable, extension in (
            (False, "nanvixd.elf", ".tar.gz"),
            (True, "nanvixd.exe", ".zip"),
        ):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for profile in ("python", "javascript", "busybox", "ci-composed"):
                    source = root / "build/dist" / profile
                    for relative_path in (executable, *required_files):
                        artifact = source / relative_path
                        artifact.parent.mkdir(parents=True, exist_ok=True)
                        artifact.write_bytes(f"{profile}:{relative_path}".encode())

                environment = {
                    "GITHUB_SHA": "0123456789abcdef",
                    "GITHUB_WORKSPACE": str(root),
                }
                with (
                    patch.dict(os.environ, environment),
                    patch.object(runner, "IS_WINDOWS", windows),
                    patch.object(runner, "run_output", return_value="1700000000"),
                ):
                    package_distributions()
                    release_directory = root / "release-distributions"
                    archives = sorted(release_directory.glob(f"*{extension}"))
                    self.assertEqual(len(archives), 4)
                    first_output = {
                        archive.name: archive.read_bytes() for archive in archives
                    }

                    package_distributions()
                    second_output = {
                        archive.name: archive.read_bytes() for archive in archives
                    }

                self.assertEqual(first_output, second_output)
                expected_names = {executable, *required_files}
                for archive in archives:
                    if windows:
                        with zipfile.ZipFile(archive) as zip_file:
                            self.assertEqual(set(zip_file.namelist()), expected_names)
                    else:
                        with tarfile.open(archive, mode="r:gz") as tar_file:
                            members = tar_file.getmembers()
                            self.assertEqual(
                                {member.name for member in members}, expected_names
                            )
                            self.assertTrue(
                                all(
                                    member.uid == 0
                                    and member.gid == 0
                                    and member.mtime == 1700000000
                                    for member in members
                                )
                            )

    def test_setup_kvm_configures_github_hosted_device(self) -> None:
        """Hosted Linux jobs configure the KVM device exposed by the image."""
        runner = self.load_runner()
        setup_kvm = cast(Callable[[], None], getattr(runner, "setup_kvm"))
        rule = (
            'KERNEL=="kvm", GROUP="kvm", MODE="0666", ' 'OPTIONS+="static_node=kvm"\n'
        )

        with (
            patch.object(runner, "IS_WINDOWS", False),
            patch.object(runner.Path, "exists", return_value=True),
            patch.object(runner.os, "access", return_value=True) as access,
            patch.object(runner, "run") as run_command,
        ):
            setup_kvm()

        access.assert_called_once_with("/dev/kvm", runner.os.R_OK | runner.os.W_OK)
        self.assertEqual(
            run_command.call_args_list,
            [
                call(
                    ("sudo", "tee", "/etc/udev/rules.d/99-kvm4all.rules"),
                    input_text=rule,
                ),
                call(("sudo", "udevadm", "control", "--reload-rules")),
                call(
                    (
                        "sudo",
                        "udevadm",
                        "trigger",
                        "--settle",
                        "--name-match=kvm",
                    )
                ),
                call(("ls", "-l", "/dev/kvm")),
            ],
        )

    def test_setup_kvm_rejects_inaccessible_device(self) -> None:
        """Hosted Linux jobs fail before trying to use inaccessible KVM."""
        runner = self.load_runner()
        setup_kvm = cast(Callable[[], None], getattr(runner, "setup_kvm"))
        ci_error = cast(type[Exception], getattr(runner, "CiError"))

        with (
            patch.object(runner, "IS_WINDOWS", False),
            patch.object(runner.Path, "exists", return_value=True),
            patch.object(runner.os, "access", return_value=False),
            patch.object(runner, "run") as run_command,
            self.assertRaisesRegex(ci_error, "cannot read and write /dev/kvm"),
        ):
            setup_kvm()

        self.assertNotIn(call(("ls", "-l", "/dev/kvm")), run_command.call_args_list)


class DistroAutoupgradeCiTests(unittest.TestCase):
    """Verify orchestration of autoupgrade validation runs."""

    ROOT = Path(__file__).parents[1]

    @classmethod
    def load_runner(cls) -> types.ModuleType:
        module_name = "_nanvix_distro_autoupgrade"
        spec = importlib.util.spec_from_file_location(
            module_name,
            cls.ROOT / "scripts/ci/distro-autoupgrade.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {module_name: module}):
            spec.loader.exec_module(module)
        return module

    def test_wait_for_ci_ignores_runs_that_predate_dispatch(self) -> None:
        """A reused PR head must not select a stale cancelled run."""
        runner = self.load_runner()
        wait_for_ci = cast(Callable[[], None], getattr(runner, "wait_for_ci"))
        completed = types.SimpleNamespace(
            returncode=0,
            stdout="completed\tsuccess\n",
        )

        with (
            patch.dict(
                os.environ,
                {"PR_NUMBER": "9", "REPOSITORY": "nanvix/distro"},
            ),
            patch.object(
                runner,
                "run_output",
                side_effect=(
                    "head-sha",
                    "automation/distro-autoupgrade",
                    "111",
                    "111",
                    "222\n111",
                ),
            ),
            patch.object(runner, "run"),
            patch.object(runner.subprocess, "run", return_value=completed) as state,
            patch.object(runner.time, "sleep") as sleep,
            patch.object(runner, "append_github_output") as append_output,
        ):
            wait_for_ci()

        sleep.assert_called_once_with(10)
        self.assertIn(
            "repos/nanvix/distro/actions/runs/222",
            state.call_args.args[0],
        )
        append_output.assert_called_once_with("head-sha", "head-sha")


if __name__ == "__main__":
    unittest.main()
