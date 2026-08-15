"""Top-level orchestrator tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nanvix_distro.sdk import ContractError
from z import (
    IncompleteReleaseSetError,
    PORTS,
    UpgradeTarget,
    _upgrade_order,
    build_nanvix_core,
    build_port,
    clear_external_port_sysroot,
    cmd_dist,
    cmd_distclean,
    cmd_menuconfig,
    cmd_run,
    cmd_test,
    cmd_upgrade,
    export_port_artifacts,
    nanvix_make_args,
    prepare_port_overlay_root,
    resolve_upgrade_targets,
    stage_local_dependency_archives,
    topological_sort,
)


class WorkflowConfigurationTests(unittest.TestCase):
    """Verify CI exercises both supported host hypervisors."""

    ROOT = Path(__file__).parents[1]

    @classmethod
    def read_project_file(cls, path: str) -> str:
        """Read a workflow or its standalone implementation script."""
        return (cls.ROOT / path).read_text(encoding="utf-8")

    def test_ci_builds_and_runs_distributions_on_linux_and_windows(self) -> None:
        """Both host jobs build the distro and smoke-test the public run command."""
        workflow = self.read_project_file(".github/workflows/self-hosted-test.yml")
        runner = self.read_project_file("scripts/ci/self-hosted-test.py")

        self.assertIn('name: "Linux Build & Test"', workflow)
        self.assertIn(
            'name: "Windows Build & Test (${{ matrix.build-type }})"', workflow
        )
        self.assertIn('name: "Smoke Test BusyBox Distribution (KVM)"', workflow)
        self.assertIn('name: "Smoke Test BusyBox Distribution (WHP)"', workflow)
        self.assertIn('run_z("build")', runner)
        self.assertIn('name: "Download Pinned Distribution Guests"', workflow)
        for package in ("busybox", "quickjs", "cpython"):
            self.assertIn(
                f"{package}-windows-x86-microvm-standalone-256mb.zip",
                runner,
            )
        self.assertIn(
            'for profile in ("busybox", "javascript", "python"):',
            runner,
        )
        self.assertIn('run_z("dist", profile)', runner)
        self.assertIn('run_z("menuconfig", "ci-composed", "--include", "all")', runner)
        self.assertIn("hashlib.sha256()", runner)
        self.assertIn("SYSROOT_DIR=N:/build/sysroot", runner)
        self.assertIn('"install",', runner)
        self.assertIn("'release' || 'debug'", workflow)
        self.assertEqual(workflow.count("matrix.build-type == 'release' &&"), 5)
        self.assertIn(
            'command = (sys.executable, "z.py", "--verbose", "run", profile)',
            runner,
        )
        self.assertIn('PYTHONUTF8: "1"', workflow)
        self.assertEqual(runner.count("NANVIX_BUSYBOX_READY"), 1)
        self.assertIn(
            "python3 scripts/ci/self-hosted-test.py create-distribution-images",
            workflow,
        )
        self.assertIn(
            r"python .\scripts\ci\self-hosted-test.py create-distribution-images",
            workflow,
        )
        self.assertNotIn("self-hosted-test.sh", workflow)
        self.assertNotIn("self-hosted-test.ps1", workflow)
        self.assertEqual(workflow.count("continue-on-error: true"), 2)

    def test_ci_uses_github_hosted_runners(self) -> None:
        """Both host jobs bootstrap on ephemeral GitHub-hosted images."""
        workflow = self.read_project_file(".github/workflows/self-hosted-test.yml")
        runner = self.read_project_file("scripts/ci/self-hosted-test.py")

        self.assertIn('name: "Distro Test"', workflow)
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertNotIn("group: Test", workflow)
        self.assertNotIn("labels: [ self-hosted", workflow)
        self.assertIn("install-rust-toolchain", workflow)
        self.assertNotIn("pre-checkout-cleanup", workflow)
        self.assertNotIn("refresh-path", workflow)
        self.assertIn('kvm_device = "/dev/kvm"', runner)
        self.assertNotIn("modprobe", runner)
        self.assertIn('("rustup", "toolchain", "install", channel)', runner)

    def test_windows_release_tests_enable_kernel_success_marker(self) -> None:
        """Release tests retain the debug marker without changing artifact logging."""
        workflow = self.read_project_file(".github/workflows/self-hosted-test.yml")
        runner = self.read_project_file("scripts/ci/self-hosted-test.py")

        self.assertIn(
            "TEST_LOG_LEVEL: ${{ matrix.build-type == 'release' && 'debug' || 'trace' }}",
            workflow,
        )
        self.assertIn("f\"LOG_LEVEL={require_env('TEST_LOG_LEVEL')}\"", runner)
        self.assertIn("f\"LOG_LEVEL={require_env('LOG_LEVEL')}\"", runner)
        self.assertLess(
            runner.index("test_arguments = ["),
            runner.index("install_arguments = ["),
        )

    def test_autoupgrade_installs_upgraded_zutils_dependencies(self) -> None:
        """Autoupgrade tests run with dependencies from the selected zutils release."""
        workflow = self.read_project_file(".github/workflows/distro-autoupgrade.yml")
        script = self.read_project_file("scripts/ci/distro-autoupgrade.py")
        upgrade = "python3 scripts/ci/distro-autoupgrade.py upgrade"
        install = "python3 scripts/ci/distro-autoupgrade.py install-python-dependencies"
        test = "python3 scripts/ci/distro-autoupgrade.py test-upgraded-release-set"

        self.assertLess(workflow.index(upgrade), workflow.index(install))
        self.assertLess(workflow.index(install), workflow.index(test))
        self.assertIn("--defer-incomplete-release-set", script)
        self.assertIn('("git", "diff", "--quiet", "HEAD", "--")', script)
        self.assertIn("github.repository == 'nanvix/distro'", workflow)
        self.assertNotIn("nanvix/nanvix-distro", workflow)
        self.assertEqual(
            workflow.count("if: steps.upgrade.outputs.changed == 'true'"),
            3,
        )

    def test_autoupgrade_retries_exact_coordinated_release_candidate(self) -> None:
        """The final consumer tier triggers an exact, preflighted SDK upgrade."""
        workflow = self.read_project_file(".github/workflows/distro-autoupgrade.yml")
        script = self.read_project_file("scripts/ci/distro-autoupgrade.py")

        self.assertIn('cron: "0 20 * * *"', workflow)
        self.assertIn("types: [distro-release-candidate]", workflow)
        self.assertIn("github.event.client_payload.sdk_version", workflow)
        self.assertIn(
            'if event_name == "repository_dispatch" and not sdk_version:',
            script,
        )
        self.assertIn('command.extend(("--sdk-version", sdk_version))', script)
        self.assertNotIn("github.event.client_payload.release_tag", workflow)

    def test_ci_publishes_eight_release_distributions_after_merges(self) -> None:
        """Main-branch pushes publish the supported distribution combinations."""
        workflow = self.read_project_file(".github/workflows/self-hosted-test.yml")
        runner = self.read_project_file("scripts/ci/self-hosted-test.py")

        self.assertIn("publish-release-distributions:", workflow)
        self.assertIn('name: "Publish Release Distribution Images"', workflow)
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && inputs.release", workflow
        )
        self.assertIn("EXPECTED_COMMIT: ${{ inputs.expected_commit }}", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertEqual(workflow.count("github.repository == 'nanvix/distro'"), 1)
        self.assertNotIn("nanvix/nanvix-distro", workflow)
        self.assertIn("prepare-release-distributions:", workflow)
        self.assertIn(
            "needs: [ prepare-release-distributions, linux-build-test, windows-build-test ]",
            workflow,
        )
        self.assertEqual(workflow.count("needs: prepare-release-distributions"), 2)
        self.assertIn("release-id: ${{ steps.release.outputs.release-id }}", workflow)
        self.assertIn(
            "release-build: ${{ steps.release.outputs.release-id != '' }}",
            workflow,
        )
        self.assertEqual(
            workflow.count(
                "RELEASE_ID: ${{ needs.prepare-release-distributions.outputs.release-id }}"
            ),
            3,
        )
        self.assertEqual(workflow.count('name: "Prepare GitHub Release"'), 1)
        self.assertEqual(runner.count('"draft": True'), 1)
        self.assertEqual(
            runner.count('require_release_id(require_env("RELEASE_ID"))'), 2
        )
        for profile in (
            '("python", "cpython")',
            '("javascript", "quickjs")',
            '("busybox", "busybox")',
            '("ci-composed", "cpython-quickjs-busybox")',
        ):
            self.assertIn(profile, runner)
        self.assertIn(
            'f"nanvix-distro-windows-x86-microvm-256mb-cpython-{commit}.zip"',
            runner,
        )
        self.assertIn(
            'f"nanvix-distro-windows-x86-microvm-256mb-quickjs-{commit}.zip"',
            runner,
        )
        self.assertIn(
            'f"nanvix-distro-windows-x86-microvm-256mb-busybox-{commit}.zip"',
            runner,
        )
        self.assertIn(
            'f"nanvix-distro-windows-x86-microvm-256mb-cpython-quickjs-busybox-{commit}.zip"',
            runner,
        )
        self.assertIn('Path("bin/nanvix.ramfs")', runner)
        self.assertIn("Expected 4 {host}release distribution images", runner)
        self.assertIn("Expected 8 release distribution images", runner)
        self.assertEqual(
            runner.count("- Windows/WHP: CPython, QuickJS, and BusyBox"), 1
        )
        self.assertIn('name: "Stage Release Distribution Images"', workflow)
        self.assertIn("https://uploads.github.com/repos/{repository}", runner)
        self.assertNotIn("/releases/tags/{release_tag}", runner)
        self.assertNotIn('name: "Upload Release Distribution Images"', workflow)
        self.assertNotIn("actions/download-artifact@v8", workflow)
        self.assertIn('release_tag = f"distro-{commit}"', runner)
        self.assertIn(
            "Release assets do not match the expected distribution set",
            runner,
        )
        self.assertIn('f"{api_root}/releases?per_page=100"', runner)
        self.assertIn('"tag_name": release_tag', runner)
        self.assertIn('"target_commitish": commit', runner)
        self.assertIn('release_url = f"{api_root}/releases/{release_id}"', runner)

    def test_workflows_invoke_only_standalone_scripts(self) -> None:
        """Workflow run steps delegate implementation to checked-in scripts."""
        workflows = (
            self.read_project_file(".github/workflows/self-hosted-test.yml"),
            self.read_project_file(".github/workflows/distro-autoupgrade.yml"),
        )

        for workflow in workflows:
            with self.subTest(workflow=workflow.splitlines()[3]):
                self.assertNotRegex(
                    workflow,
                    re.compile(r"^\s+run:\s*[|>]", re.MULTILINE),
                )
                run_commands = re.findall(r"^\s+run: (.+)$", workflow, re.MULTILINE)
                self.assertTrue(run_commands)
                self.assertTrue(
                    all(
                        "scripts/ci/" in command or "scripts\\ci\\" in command
                        for command in run_commands
                    )
                )
                self.assertTrue(all(".py" in command for command in run_commands))

    def test_release_helper_rejects_invalid_ids(self) -> None:
        """Release API identifiers must be valid before reaching task callers."""
        helper = self.ROOT / "scripts/ci/self-hosted-test.py"
        cases = (
            (
                "find-draft-release",
                [
                    {
                        "tag_name": "distro-commit",
                        "target_commitish": "commit",
                        "draft": True,
                    }
                ],
                ("distro-commit", "commit"),
            ),
            (
                "find-release-asset-ids",
                [{"name": "distribution.tar.gz", "id": "123"}],
                ("distribution.tar.gz",),
            ),
        )

        with tempfile.TemporaryDirectory() as temporary:
            for command, payload, arguments in cases:
                with self.subTest(command=command):
                    response = Path(temporary) / f"{command}.json"
                    response.write_text(json.dumps(payload), encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(helper),
                            command,
                            str(response),
                            *arguments,
                        ],
                        capture_output=True,
                        check=False,
                        text=True,
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("has invalid id", result.stderr)


class IntegrationDocumentationTests(unittest.TestCase):
    """Keep package integration guides synchronized with their sources of truth."""

    def test_documented_package_versions_match_port_manifests(self) -> None:
        """Every supported package and version comes from its checked-out manifest."""
        root = Path(__file__).parents[1]
        packages_doc = (root / "doc/packages.md").read_text(encoding="utf-8")
        rows = re.findall(
            r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|",
            packages_doc,
            flags=re.MULTILINE,
        )
        documented_versions = {
            package.lower(): version
            for package, version in rows
            if package.lower() in PORTS
        }

        self.assertEqual(set(documented_versions), set(PORTS))
        for name, (relative_path, _) in PORTS.items():
            with self.subTest(package=name):
                manifest_path = root / relative_path / ".nanvix/nanvix.toml"
                with manifest_path.open("rb") as manifest_file:
                    package = tomllib.load(manifest_file)["package"]
                self.assertEqual(package["name"], name)
                self.assertEqual(documented_versions[name], package["version"])

    def test_documented_package_order_matches_build_order(self) -> None:
        """The build guide reflects the orchestrator's deterministic order."""
        build_doc = (Path(__file__).parents[1] / "doc/build.md").read_text(
            encoding="utf-8"
        )
        order_block = re.search(
            r"The current package order is:\s*```text\s*(.*?)\s*```",
            build_doc,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(order_block)
        assert order_block is not None
        documented_order = tuple(
            name.strip() for name in order_block.group(1).replace("\n", " ").split(",")
        )

        self.assertEqual(documented_order, tuple(topological_sort(PORTS)))


class DependencyGraphTests(unittest.TestCase):
    """Verify package ordering for local/offline dependency overlays."""

    def test_dependencies_precede_consumers(self) -> None:
        """Every dependency is built before the port that consumes it."""
        order = topological_sort(PORTS)
        positions = {name: index for index, name in enumerate(order)}

        for name, (_, dependencies) in PORTS.items():
            for dependency in dependencies:
                with self.subTest(port=name, dependency=dependency):
                    self.assertLess(positions[dependency], positions[name])

    def test_cpython_dependencies_are_complete(self) -> None:
        """The local DAG contains all dependencies in CPython's SDK manifest."""
        dependencies = set(PORTS["cpython"][1])
        self.assertEqual(
            dependencies,
            {
                "bzip2",
                "libffi",
                "libxml2",
                "libxslt",
                "lxml",
                "openssl",
                "sqlite",
                "xz",
                "zlib",
            },
        )


class NanvixCommandTests(unittest.TestCase):
    """Verify make arguments used by the pinned Nanvix runtime."""

    def test_preserves_only_timestamps_when_staging_cargo_artifacts(self) -> None:
        """Relocated Cargo targets may not support permission preservation."""
        with (
            patch("z.IS_WINDOWS", False),
            patch("z.run_cmd") as run_command,
        ):
            build_nanvix_core()

        command = run_command.call_args.args[0]
        self.assertIn("CP_CMD=cp -f --preserve=timestamps", command)

    def test_uses_nanvix_copy_default_on_windows(self) -> None:
        """Native Windows builds must not pass the POSIX-only copy override."""
        with patch("z.IS_WINDOWS", True):
            arguments = nanvix_make_args()

        self.assertFalse(any(argument.startswith("CP_CMD=") for argument in arguments))
        sysroot = next(
            argument.removeprefix("SYSROOT_DIR=")
            for argument in arguments
            if argument.startswith("SYSROOT_DIR=")
        )
        self.assertNotIn("\\", sysroot)

    def test_disables_sccache_on_wsl_windows_mount(self) -> None:
        """DrvFS builds use an ext4 target and bypass its incompatible cache."""
        with (
            patch("z.IS_WINDOWS", False),
            patch("z._filesystem_type", return_value="9p"),
        ):
            arguments = nanvix_make_args()

        self.assertIn("SCCACHE=", arguments)
        self.assertIn("RUSTC_WRAPPER=", arguments)
        objects = next(
            argument.removeprefix("OBJECTS_DIR=")
            for argument in arguments
            if argument.startswith("OBJECTS_DIR=")
        )
        self.assertIn("/.cache/nanvix-distro/", objects.replace("\\", "/"))
        self.assertIn(f"CARGO_TARGET_DIR={objects}", arguments)

    def test_keeps_sccache_on_native_linux_filesystem(self) -> None:
        """Native Linux filesystems retain Nanvix's automatic compiler cache."""
        with (
            patch("z.IS_WINDOWS", False),
            patch("z._filesystem_type", return_value="ext4"),
        ):
            arguments = nanvix_make_args()

        self.assertNotIn("SCCACHE=", arguments)
        self.assertNotIn("RUSTC_WRAPPER=", arguments)
        self.assertFalse(
            any(argument.startswith("OBJECTS_DIR=") for argument in arguments)
        )

    def test_preserves_only_timestamps_when_testing_cargo_artifacts(self) -> None:
        """Core tests use the relocated Cargo target after CI cleanup."""
        args = argparse.Namespace(dry_run=True, verbose=False)
        with (
            patch("z.IS_WINDOWS", False),
            patch("z.load_sdk_contract"),
            patch("z.validate_sdk_release_set"),
            patch("z.run_cmd") as run_command,
        ):
            cmd_test(args)

        run_command.assert_called_once()
        command = run_command.call_args.args[0]
        self.assertIn("CP_CMD=cp -f --preserve=timestamps", command)
        self.assertFalse(any(argument.startswith("LOG_LEVEL=") for argument in command))
        self.assertFalse(
            any(argument.startswith("SYSROOT_DIR=") for argument in command)
        )


class PortBuildContractTests(unittest.TestCase):
    """Verify the local artifact contract used by current zutils releases."""

    def test_setup_uses_unified_runtime_overlay_without_removed_flag(self) -> None:
        """Offline setup receives one root containing runtime and dependency data."""
        runtime = Path("overlay") / "sysroot"
        with (
            patch("z.RUNTIME_SYSROOT_DIR", runtime),
            patch("z.stage_local_dependency_archives"),
            patch("z.export_port_artifacts"),
            patch("z.run_cmd") as run_command,
        ):
            build_port("zlib")

        setup_command = run_command.call_args_list[0].args[0]
        self.assertEqual(
            setup_command[2:],
            [
                "setup",
                "--offline",
                "--with-nanvix",
                str(runtime),
            ],
        )
        self.assertNotIn("--sysroot-path", setup_command)

    def test_runtime_overlay_links_documented_dependency_exports(self) -> None:
        """The zutils overlay sees build/deps without relocating public outputs."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "sysroot"
            dependencies = root / "deps"
            runtime.mkdir()
            dependencies.mkdir()
            (dependencies / "marker").write_text("local", encoding="utf-8")
            dependency_link = runtime / "deps"

            try:
                with (
                    patch("z.RUNTIME_SYSROOT_DIR", runtime),
                    patch("z.DEPS_DIR", dependencies),
                ):
                    prepare_port_overlay_root()

                self.assertEqual(dependency_link.resolve(), dependencies.resolve())
                self.assertEqual(
                    (dependency_link / "marker").read_text(encoding="utf-8"),
                    "local",
                )
            finally:
                if dependency_link.is_symlink():
                    dependency_link.unlink()
                elif dependency_link.exists():
                    dependency_link.rmdir()

    def test_setup_discards_only_legacy_external_sysroot_state(self) -> None:
        """An old shared sysroot cannot make zutils copy files onto themselves."""
        with tempfile.TemporaryDirectory() as temporary:
            port = Path(temporary) / "zlib"
            nanvix_dir = port / ".nanvix"
            nanvix_dir.mkdir(parents=True)
            env_path = nanvix_dir / "env.json"
            env_path.write_text(
                json.dumps(
                    {
                        "NANVIX_SYSROOT": str(Path(temporary) / "shared-sysroot"),
                        "NANVIX_MACHINE": "microvm",
                    }
                ),
                encoding="utf-8",
            )

            clear_external_port_sysroot(port)

            config = json.loads(env_path.read_text(encoding="utf-8"))
            self.assertNotIn("NANVIX_SYSROOT", config)
            self.assertEqual(config["NANVIX_MACHINE"], "microvm")


class PortArtifactExportTests(unittest.TestCase):
    """Verify compatibility with old and new port staging layouts."""

    @staticmethod
    def _load_cpython_helper(name: str) -> types.ModuleType:
        root = Path(__file__).parents[1] / "usr/bin/cpython/.nanvix"
        source_root = root / "src"
        packaged = (source_root / "config.py").is_file()
        if not packaged:
            source_root = root

        package_name = "src" if packaged else "cpython_nanvix"
        config_name = f"{package_name}.config" if packaged else "config"
        config_spec = importlib.util.spec_from_file_location(
            config_name, source_root / "config.py"
        )
        assert config_spec is not None and config_spec.loader is not None
        config_module = importlib.util.module_from_spec(config_spec)

        modules = {config_name: config_module}
        if packaged:
            package = types.ModuleType(package_name)
            setattr(package, "__path__", [str(source_root)])
            modules[package_name] = package

        if name == "config":
            with patch.dict("sys.modules", modules):
                config_spec.loader.exec_module(config_module)
            return config_module

        module_name = f"{package_name}.{name}" if packaged else f"{package_name}_{name}"
        module_spec = importlib.util.spec_from_file_location(
            module_name, source_root / f"{name}.py"
        )
        assert module_spec is not None and module_spec.loader is not None
        module = importlib.util.module_from_spec(module_spec)
        modules[module_name] = module
        if name == "ramfs":
            fake_zutils = types.ModuleType("nanvix_zutil")
            setattr(
                fake_zutils,
                "paths",
                types.SimpleNamespace(
                    out_dir=lambda: Path("mounted-workspace/.nanvix/out")
                ),
            )
            modules["nanvix_zutil"] = fake_zutils

        zutils_source = Path(__file__).parents[1] / "usr/lib/zutils/src"
        with (
            patch.object(sys, "path", [str(zutils_source), *sys.path]),
            patch.dict("sys.modules", modules),
        ):
            config_spec.loader.exec_module(config_module)
            module_spec.loader.exec_module(module)
        return module

    def test_libffi_configures_out_of_tree_without_recursive_reexec(self) -> None:
        """Autoconf must not rename its active config.log across WSL's 9p mount."""
        makefile = (
            Path(__file__).parents[1] / "usr/lib/libffi/Makefile.nanvix"
        ).read_text(encoding="utf-8")

        self.assertIn("\tcd $(BUILD_DIR) && \\\n\tCC=", makefile)
        self.assertIn('CC="$(CC)" \\\n', makefile)
        self.assertIn("../configure --srcdir=.. \\\n", makefile)
        self.assertNotIn("\n\t./configure --host=", makefile)
        self.assertIn("$(MAKE) -C $(BUILD_DIR)", makefile)
        self.assertIn("rm -rf $(BUILD_DIR)", makefile)

    def test_cpython_isolates_case_insensitive_workspaces(self) -> None:
        """The `python` output must not collide with CPython's `Python/` sources."""
        module = self._load_cpython_helper("config")

        with patch.object(module, "IS_WINDOWS", False):
            with patch.object(Path, "samefile", return_value=False):
                self.assertFalse(module.requires_isolated_workspace(Path("workspace")))

            with patch.object(Path, "samefile", return_value=True):
                self.assertTrue(module.requires_isolated_workspace(Path("workspace")))

        with patch.object(module, "IS_WINDOWS", True):
            self.assertTrue(module.requires_isolated_workspace(Path("workspace")))

    def test_cpython_ramfs_uses_host_temporary_directory(self) -> None:
        """Random-access RAMFS generation stays off Windows-mounted WSL paths."""
        ramfs_module = self._load_cpython_helper("ramfs")
        config_module = ramfs_module.config

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            staging = temporary_root / "staging"
            staging.mkdir()
            nanvix_home = temporary_root / "nanvix"
            tool = nanvix_home / "bin" / config_module.mkramfs_binary()
            tool.parent.mkdir(parents=True)
            tool.write_bytes(b"tool")
            output = temporary_root / "published.img"

            def create_image(command: list[str], **_: object) -> None:
                temporary_image = Path(command[2])
                self.assertNotEqual(
                    temporary_image.parent, ramfs_module.paths.out_dir()
                )
                temporary_image.write_bytes(b"ramfs")

            with patch.object(ramfs_module.subprocess, "run", side_effect=create_image):
                result = ramfs_module.build_image(staging, nanvix_home, output)

            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), b"ramfs")

    def test_cpython_test_ramfs_stages_in_host_temp(self) -> None:
        """Test RAMFS trimming does not duplicate the stdlib on WSL 9p."""
        root = Path(__file__).parents[1] / "usr/bin/cpython/.nanvix"
        helper_path = root / "_test.py"
        if not helper_path.is_file():
            helper_path = root / "src/lib.py"
        helper = helper_path.read_text(encoding="utf-8")

        self.assertIn('TemporaryDirectory(prefix="cpython_test_ramfs_")', helper)
        self.assertNotIn('paths.out_dir() / "_ramfs_cache"', helper)

    def test_merges_regular_and_dev_staging_trees(self) -> None:
        """Runtime and development artifacts form one dependency payload."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            port = root / "port"
            regular = port / ".nanvix" / "out" / "staging" / "regular"
            development = port / ".nanvix" / "out" / "staging" / "dev"
            (regular / "bin").mkdir(parents=True)
            (development / "lib").mkdir(parents=True)
            (regular / "bin" / "tool.elf").write_bytes(b"runtime")
            (development / "lib" / "libtool.a").write_bytes(b"development")

            with patch("z.DEPS_DIR", root / "deps"):
                export_port_artifacts("quickjs", port)

            self.assertEqual(
                (root / "deps" / "quickjs" / "bin" / "tool.elf").read_bytes(),
                b"runtime",
            )
            self.assertEqual(
                (root / "deps" / "quickjs" / "lib" / "libtool.a").read_bytes(),
                b"development",
            )

    def test_falls_back_to_legacy_release_tree(self) -> None:
        """Ports pinned before magic-path staging remain buildable."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            port = root / "port"
            release = port / ".nanvix" / "out" / "release" / "lib"
            release.mkdir(parents=True)
            (release / "libz.a").write_bytes(b"legacy")

            with patch("z.DEPS_DIR", root / "deps"):
                export_port_artifacts("zlib", port)

            self.assertEqual(
                (root / "deps" / "zlib" / "lib" / "libz.a").read_bytes(),
                b"legacy",
            )

    def test_cpython_prefers_split_staging(self) -> None:
        """Upgraded CPython exports its regular magic-path payload."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            port = root / "port"
            regular = port / ".nanvix" / "out" / "staging" / "regular"
            regular.mkdir(parents=True)
            (regular / "cpython-ramfs.img").write_bytes(b"runtime")

            with patch("z.DEPS_DIR", root / "deps"):
                export_port_artifacts("cpython", port)

            self.assertEqual(
                (root / "deps" / "cpython" / "cpython-ramfs.img").read_bytes(),
                b"runtime",
            )

    def test_cpython_retains_legacy_export_root(self) -> None:
        """Pinned CPython still exports its legacy sysroot package."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            port = root / "port"
            legacy = port / ".nanvix" / "out" / "release" / "sysroot-pkg"
            legacy.mkdir(parents=True)
            (legacy / "cpython-ramfs.img").write_bytes(b"legacy")

            with patch("z.DEPS_DIR", root / "deps"):
                export_port_artifacts("cpython", port)

            self.assertEqual(
                (root / "deps" / "cpython" / "cpython-ramfs.img").read_bytes(),
                b"legacy",
            )

    def test_local_archive_matches_dependency_asset_name(self) -> None:
        """Local overlays use the canonical development-archive pattern."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dependency = root / "deps" / "lxml" / "python-packages" / "lxml"
            dependency.mkdir(parents=True)
            (dependency / "__init__.py").write_text("", encoding="utf-8")
            port = root / "cpython"

            with (
                patch("z.DEPS_DIR", root / "deps"),
                patch.dict(
                    "z.PORTS",
                    {"cpython": ("usr/bin/cpython", ["lxml"])},
                    clear=True,
                ),
            ):
                stage_local_dependency_archives("cpython", port)

            cache = port / ".nanvix" / "cache"
            legacy_archives = list(cache.glob("lxml-microvm-*"))
            canonical_archives = list(
                cache.glob("lxml-*-microvm-standalone-256mb-dev.*")
            )
            self.assertEqual(
                [archive.name for archive in legacy_archives],
                ["lxml-microvm-standalone-256mb.zz-local.tar.gz"],
            )
            self.assertEqual(
                [archive.name for archive in canonical_archives],
                ["lxml-zz-local-microvm-standalone-256mb-dev.tar.gz"],
            )


class DistributionCommandTests(unittest.TestCase):
    """Verify distribution image prerequisite diagnostics."""

    def test_missing_build_artifact_reports_build_command(self) -> None:
        """A clean checkout points users to the prerequisite build command."""
        args = argparse.Namespace(target="busybox", dry_run=False, verbose=False)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch("z.RUNTIME_SYSROOT_DIR", root / "runtime"),
                patch("z.DEPS_DIR", root / "deps"),
                patch("z.DIST_DIR", root / "dist"),
                patch("z.STAGING_DIR", root / "staging"),
                patch("z.load_sdk_contract"),
                patch("z.validate_sdk_release_set"),
                patch("z.error") as report_error,
                self.assertRaises(SystemExit) as exit,
            ):
                cmd_dist(args)

        self.assertEqual(exit.exception.code, 1)
        messages = [call.args[0] for call in report_error.call_args_list]
        self.assertIn("required runtime artifact not found", messages[0])
        self.assertIn("z.py build", messages[1])


class MenuconfigCommandTests(unittest.TestCase):
    """Verify top-level menuconfig namespace rules."""

    def test_rejects_builtin_profile_name(self) -> None:
        """Generated profiles cannot overwrite a built-in image output."""
        args = argparse.Namespace(
            name="python",
            include=["busybox"],
            dry_run=True,
            verbose=False,
        )

        with patch("z.error") as report_error, self.assertRaises(SystemExit) as exit:
            cmd_menuconfig(args)

        self.assertEqual(exit.exception.code, 1)
        report_error.assert_called_once()
        self.assertIn("reserved", report_error.call_args.args[0])

    def test_distclean_preserves_generated_profiles(self) -> None:
        """Reusable menuconfig inputs survive removal of build artifacts."""
        args = argparse.Namespace(dry_run=True, verbose=False)

        with patch("z.run_cmd") as run_command:
            cmd_distclean(args)

        clean_command = run_command.call_args_list[-1].args[0]
        self.assertIn("--exclude=distributions/", clean_command)

    def test_distclean_removes_external_wsl_objects(self) -> None:
        """WSL's ext4 Cargo target is part of the distribution build state."""
        args = argparse.Namespace(dry_run=False, verbose=False)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / "build"
            external = root / "cache" / "target"
            build.mkdir()
            external.mkdir(parents=True)

            with (
                patch("z.BUILD_DIR", build),
                patch("z._external_nanvix_objects_dir", return_value=external),
                patch("z._clean_cpython_external_state"),
                patch("z.run_cmd"),
            ):
                cmd_distclean(args)

            self.assertFalse(build.exists())
            self.assertFalse(external.exists())

    def test_distclean_removes_cpython_external_state(self) -> None:
        """Top-level cleanup also removes CPython's named Docker volume."""
        args = argparse.Namespace(dry_run=False, verbose=False)

        with (
            patch("z.BUILD_DIR", Path("missing-build-directory")),
            patch("z._external_nanvix_objects_dir", return_value=None),
            patch("z._clean_cpython_external_state") as clean_cpython,
            patch("z.run_cmd"),
        ):
            cmd_distclean(args)

        clean_cpython.assert_called_once_with()

    def test_distclean_dry_run_preserves_cpython_external_state(self) -> None:
        """Dry-run reports but does not remove external Docker state."""
        args = argparse.Namespace(dry_run=True, verbose=False)

        with (
            patch("z._clean_cpython_external_state") as clean_cpython,
            patch("z.run_cmd"),
        ):
            cmd_distclean(args)

        clean_cpython.assert_not_called()


class RunCommandTests(unittest.TestCase):
    """Verify distribution launches select the native host backend."""

    def test_runs_with_whp_on_windows(self) -> None:
        """Windows launches the PE daemon and omits the Unix console device."""
        args = argparse.Namespace(target="busybox", dry_run=True, verbose=False)
        with (
            patch("z.IS_WINDOWS", True),
            patch("z.run_cmd") as run_command,
        ):
            cmd_run(args)

        command = run_command.call_args.args[0]
        self.assertEqual(Path(command[0]).name, "nanvixd.exe")
        self.assertNotIn("/dev/stdout", command)
        self.assertTrue(run_command.call_args.kwargs["interactive"])

    def test_runs_with_kvm_on_linux(self) -> None:
        """Linux launches the ELF daemon with console output attached."""
        args = argparse.Namespace(target="busybox", dry_run=True, verbose=False)
        with (
            patch("z.IS_WINDOWS", False),
            patch("z.run_cmd") as run_command,
        ):
            cmd_run(args)

        command = run_command.call_args.args[0]
        self.assertEqual(Path(command[0]).name, "nanvixd.elf")
        self.assertIn("/dev/stdout", command)


class UpgradeCommandTests(unittest.TestCase):
    """Verify distro release selection at the upgrade command boundary."""

    def test_excludes_tooling_submodule_from_release_set(self) -> None:
        """Repository tooling is pinned independently of distro releases."""
        contract = MagicMock()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch("z.REPO_ROOT", root),
                patch(
                    "z.list_submodule_paths",
                    return_value=[".github/prompts"],
                ),
                patch(
                    "z.list_submodules",
                    return_value=[
                        (".github/prompts", ".github/prompts", "prompts-url")
                    ],
                ),
                patch("z.run_cmd") as run_command,
            ):
                order = _upgrade_order(root)
                targets = resolve_upgrade_targets(contract, verbose=False)

        self.assertEqual(order, [])
        self.assertEqual(targets, {})
        run_command.assert_not_called()

    def test_identifies_missing_sdk_port_release_as_incomplete(self) -> None:
        """A missing exact SDK-qualified port tag is a propagating release set."""
        contract = MagicMock()
        contract.release_coordinate = "0.21.43-sdk.1"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "usr/lib/libxml2/.git").mkdir(parents=True)
            with (
                patch("z.REPO_ROOT", root),
                patch(
                    "z.list_submodules",
                    return_value=[("libxml2", "usr/lib/libxml2", "url")],
                ),
                patch("z.run_cmd"),
                patch(
                    "z._latest_matching_tag",
                    side_effect=ContractError("libxml2 tag missing"),
                ),
                self.assertRaises(IncompleteReleaseSetError),
            ):
                resolve_upgrade_targets(contract, verbose=False)

    def test_defers_incomplete_release_set_when_requested(self) -> None:
        """Automation may retry later while SDK-qualified port tags propagate."""
        args = argparse.Namespace(
            dry_run=False,
            verbose=False,
            sdk_version=None,
            allow_downgrade=False,
            defer_incomplete_release_set=True,
        )
        fetched = MagicMock()
        fetched.contract.sdk_version = "v0.21.43-sdk.1"
        fetched.contract.image_digest = f"sha256:{'a' * 64}"

        with (
            patch("z._upgrade_order", return_value=["usr/lib/libxml2"]),
            patch("z.fetch_sdk_contract", return_value=fetched),
            patch(
                "z.resolve_upgrade_targets",
                side_effect=IncompleteReleaseSetError("libxml2 tag missing"),
            ),
        ):
            cmd_upgrade(args)

        fetched.write.assert_not_called()

    def test_does_not_defer_other_contract_failures(self) -> None:
        """Defer mode must not hide invalid SDK contracts or release metadata."""
        args = argparse.Namespace(
            dry_run=False,
            verbose=False,
            sdk_version=None,
            allow_downgrade=False,
            defer_incomplete_release_set=True,
        )
        fetched = MagicMock()
        fetched.contract.sdk_version = "v0.21.43-sdk.1"
        fetched.contract.image_digest = f"sha256:{'a' * 64}"

        with (
            patch("z._upgrade_order", return_value=["usr/lib/libxml2"]),
            patch("z.fetch_sdk_contract", return_value=fetched),
            patch(
                "z.resolve_upgrade_targets",
                side_effect=ContractError("invalid release metadata"),
            ),
            self.assertRaises(SystemExit) as raised,
        ):
            cmd_upgrade(args)

        self.assertEqual(raised.exception.code, 1)

    def test_rejects_release_behind_current_pin(self) -> None:
        """Automatic upgrades must not silently roll back a module."""
        contract = MagicMock()
        contract.release_coordinate = "0.21.9-sdk.1"
        current = "b" * 40
        release = "a" * 40

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "usr/bin/cpython/.git").mkdir(parents=True)
            with (
                patch("z.REPO_ROOT", root),
                patch(
                    "z.list_submodules",
                    return_value=[("cpython", "usr/bin/cpython", "url")],
                ),
                patch("z.run_cmd"),
                patch(
                    "z._latest_matching_tag",
                    return_value=("3.12.3-nanvix-0.21.9-sdk.1", release),
                ),
                patch("z._validate_port_at_commit"),
                patch("z._git_output", return_value=current),
                patch("z._is_ancestor", return_value=True),
            ):
                with self.assertRaisesRegex(ContractError, "refusing to downgrade"):
                    resolve_upgrade_targets(contract, verbose=False)

    def test_allows_explicit_downgrade(self) -> None:
        """Operators may explicitly select an older coherent release set."""
        contract = MagicMock()
        contract.release_coordinate = "0.21.9-sdk.1"
        release = "a" * 40

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "usr/bin/cpython/.git").mkdir(parents=True)
            with (
                patch("z.REPO_ROOT", root),
                patch(
                    "z.list_submodules",
                    return_value=[("cpython", "usr/bin/cpython", "url")],
                ),
                patch("z.run_cmd"),
                patch(
                    "z._latest_matching_tag",
                    return_value=("3.12.3-nanvix-0.21.9-sdk.1", release),
                ),
                patch("z._validate_port_at_commit"),
                patch("z._reject_downgrade") as reject_downgrade,
            ):
                targets = resolve_upgrade_targets(
                    contract,
                    verbose=False,
                    allow_downgrade=True,
                )

        self.assertEqual(targets["usr/bin/cpython"].commit, release)
        reject_downgrade.assert_not_called()

    def test_fetches_latest_completed_sdk_release(self) -> None:
        """Upgrade falls back to the latest completed SDK release."""
        fetched, fetch_contract = self._run_dry_run(None)

        fetch_contract.assert_called_once_with(None, token="test-token")
        fetched.write.assert_not_called()

    def test_fetches_requested_sdk_release(self) -> None:
        """Upgrade honors an exact SDK release when one is requested."""
        fetched, fetch_contract = self._run_dry_run("v0.20.0-sdk.2")

        fetch_contract.assert_called_once_with(
            "v0.20.0-sdk.2",
            token="test-token",
        )
        fetched.write.assert_not_called()

    def _run_dry_run(self, sdk_version: str | None) -> tuple[MagicMock, MagicMock]:
        args = argparse.Namespace(
            dry_run=True,
            verbose=False,
            sdk_version=sdk_version,
            allow_downgrade=False,
            defer_incomplete_release_set=False,
        )
        fetched = MagicMock()
        fetched.contract.sdk_version = "v0.21.0-sdk.1"
        fetched.contract.image_digest = f"sha256:{'a' * 64}"
        target = UpgradeTarget(
            path="nanvix",
            ref="v0.21.0",
            commit="b" * 40,
        )

        with (
            patch.dict("z.os.environ", {"GH_TOKEN": "test-token"}, clear=True),
            patch("z._upgrade_order", return_value=["nanvix"]),
            patch("z.fetch_sdk_contract", return_value=fetched) as fetch_contract,
            patch("z.resolve_upgrade_targets", return_value={"nanvix": target}),
        ):
            cmd_upgrade(args)

        return fetched, fetch_contract


if __name__ == "__main__":
    unittest.main()
