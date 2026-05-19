"""Tests for menuconfig-style distribution composition."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from nanvix_distro.composer import (
    ComposerError,
    component_titles,
    components_from_profile,
    normalize_components,
    render_profile,
    write_distribution,
)
from nanvix_distro.profile import load_profile


class ComposerTests(unittest.TestCase):
    """Verify deterministic component selection and generated files."""

    def test_normalizes_binary_aliases_without_implicit_base(self) -> None:
        """Binary aliases are accepted without implicitly enabling BusyBox."""
        self.assertEqual(
            normalize_components(("python,quickjs",)),
            frozenset({"python", "javascript"}),
        )
        self.assertEqual(normalize_components(("cpython",)), frozenset({"python"}))
        self.assertEqual(
            component_titles(("python",)),
            ("CPython",),
        )

    def test_all_selects_every_component(self) -> None:
        """The all shorthand enables the complete curated component set."""
        self.assertEqual(
            normalize_components(("all",)),
            frozenset({"busybox", "python", "javascript"}),
        )

    def test_rejects_unknown_component(self) -> None:
        """Unknown package names fail instead of producing incomplete profiles."""
        with self.assertRaisesRegex(ComposerError, "unknown component"):
            normalize_components(("ruby",))

    def test_rejects_empty_binary_selection(self) -> None:
        """A distribution must select at least one executable package."""
        with self.assertRaisesRegex(ComposerError, "at least one binary"):
            normalize_components(())

    def test_profiles_can_boot_without_busybox(self) -> None:
        """CPython and QuickJS can each be the only selected boot binary."""
        for component, source in (
            ("cpython", "package:cpython"),
            ("quickjs", "package:quickjs"),
        ):
            with (
                self.subTest(component=component),
                tempfile.TemporaryDirectory() as temp,
            ):
                profile_path = Path(temp) / "profile.toml"
                profile_path.write_text(
                    render_profile("standalone", (component,)),
                    encoding="utf-8",
                )
                profile = load_profile(profile_path)

                self.assertNotIn(
                    "package:busybox", profile_path.read_text(encoding="utf-8")
                )
                self.assertIn(
                    source,
                    {program.artifact.source for program in profile.programs},
                )

    def test_uses_one_boot_binary_without_busybox(self) -> None:
        """Additional binaries are staged instead of becoming competing init processes."""
        with tempfile.TemporaryDirectory() as temp:
            profile_path = Path(temp) / "profile.toml"
            profile_path.write_text(
                render_profile("interpreters", ("cpython", "quickjs")),
                encoding="utf-8",
            )
            profile = load_profile(profile_path)

            self.assertEqual(
                tuple(program.artifact.source for program in profile.programs[3:]),
                ("package:cpython",),
            )
            self.assertIn(
                ("package:quickjs", "/bin/qjs"),
                {
                    (entry.artifact.source, str(entry.destination))
                    for entry in profile.ramfs_entries
                },
            )
            self.assertEqual(
                components_from_profile(profile_path),
                frozenset({"python", "javascript"}),
            )

    def test_writes_and_reloads_distribution_without_busybox(self) -> None:
        """A direct interpreter distribution does not need a guest init script."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = write_distribution(root, "standalone", ("busybox",))
            assert previous.init_path is not None
            self.assertTrue(previous.init_path.is_file())

            generated = write_distribution(root, "standalone", ("cpython",))

            self.assertIsNone(generated.init_path)
            self.assertFalse(previous.init_path.exists())
            self.assertEqual(
                components_from_profile(generated.profile_path),
                frozenset({"python"}),
            )

    def test_rejects_unsafe_distribution_name(self) -> None:
        """Generated distributions cannot escape the configured output root."""
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "unsupported characters"):
                write_distribution(Path(temp), "../outside", ("busybox",))

    def test_rendered_profile_is_valid(self) -> None:
        """A generated all-components profile parses into the expected layers."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile_path = root / "profile.toml"
            profile_path.write_text(
                render_profile("complete", ("all",)),
                encoding="utf-8",
            )
            profile = load_profile(profile_path)

            sources = {entry.artifact.source for entry in profile.ramfs_entries}
            destinations = {str(entry.destination) for entry in profile.ramfs_entries}
            self.assertEqual(
                sources,
                {"profile", "package:busybox", "package:cpython", "package:quickjs"},
            )
            self.assertTrue(
                {"/bin/busybox", "/bin/python3", "/bin/qjs"} <= destinations
            )

    def test_write_and_reload_distribution(self) -> None:
        """BusyBox compositions write an init script and restore selections."""
        with tempfile.TemporaryDirectory() as temp:
            generated = write_distribution(
                Path(temp),
                "developer",
                ("busybox", "python"),
            )

            self.assertTrue(generated.profile_path.is_file())
            self.assertIsNotNone(generated.init_path)
            assert generated.init_path is not None
            self.assertTrue(generated.init_path.is_file())
            if os.name != "nt":
                self.assertTrue(generated.init_path.stat().st_mode & 0o100)
            self.assertNotIn(b"\r\n", generated.profile_path.read_bytes())
            self.assertNotIn(b"\r\n", generated.init_path.read_bytes())
            self.assertEqual(
                components_from_profile(generated.profile_path),
                frozenset({"busybox", "python"}),
            )
            self.assertIn(
                "Included: BusyBox, CPython",
                generated.init_path.read_text(encoding="utf-8"),
            )

    def test_rejects_symlinked_output_parent_outside_root(self) -> None:
        """A generated init script cannot follow rootfs outside its distribution."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "distributions"
            directory = root / "developer"
            outside = Path(temp) / "outside"
            directory.mkdir(parents=True)
            outside.mkdir()
            try:
                (directory / "rootfs").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            with self.assertRaisesRegex(ComposerError, "escapes"):
                write_distribution(root, "developer", ("busybox", "python"))

            self.assertFalse((directory / "profile.toml").exists())
            self.assertFalse((outside / "init").exists())


if __name__ == "__main__":
    unittest.main()
