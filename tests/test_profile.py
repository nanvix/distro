"""Distribution profile and image-plan tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanvix_distro.image import ArtifactRoots, ImageError, prepare_image
from nanvix_distro.profile import ProfileError, encode_command_line, load_profile

REPO_ROOT = Path(__file__).parents[1]


class ProfileTests(unittest.TestCase):
    """Validate the built-in profile schema and wire encoding."""

    def test_named_profiles_load(self) -> None:
        """Every built-in profile parses and declares the standard daemons."""
        for name in ("busybox", "python", "javascript"):
            with self.subTest(profile=name):
                profile = load_profile(REPO_ROOT / "profiles" / f"{name}.toml")
                self.assertEqual(profile.name, name)
                self.assertEqual(
                    tuple(program.argv[0] for program in profile.programs[:3]),
                    ("procd", "memd", "vfsd"),
                )

    def test_busybox_init_is_first_class(self) -> None:
        """The BusyBox profile expands its guest init section into image inputs."""
        profile = load_profile(REPO_ROOT / "profiles" / "busybox.toml")

        self.assertEqual(profile.programs[-1].argv, ("ash", "/init"))
        self.assertEqual(
            tuple(str(path) for path in profile.ramfs_directories),
            ("/tmp",),
        )
        self.assertEqual(str(profile.ramfs_entries[-1].destination), "/init")
        self.assertEqual(
            str(profile.ramfs_entries[-1].artifact.path),
            "rootfs/busybox/init",
        )

    def test_command_line_encoding(self) -> None:
        """Arguments and environment use Nanvix's escaped semicolon wire format."""
        encoded = encode_command_line(
            ("ash", "/init"),
            (("PATH", "/bin:/usr/bin"), ("VALUE", "left;right")),
        )
        self.assertEqual(
            encoded,
            r"ash /init;PATH=/bin:/usr/bin VALUE=left\;right",
        )

    def test_argument_with_whitespace_is_rejected(self) -> None:
        """Boot arguments cannot contain spaces because Nanvix splits on spaces."""
        with self.assertRaisesRegex(ProfileError, "whitespace"):
            encode_command_line(("one argument",))

    def test_kernel_args_with_carriage_return_are_rejected(self) -> None:
        """Kernel arguments must remain on one line after TOML decoding."""
        with tempfile.TemporaryDirectory() as temp:
            profile_path = Path(temp) / "invalid.toml"
            profile_path.write_text(
                """
name = "invalid"
kernel-args = "first\\rsecond"
[[program]]
source = "runtime"
path = "bin/kernel.elf"
argv = ["invalid"]
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfileError, "single line"):
                load_profile(profile_path)

    def test_traversing_profile_path_is_rejected(self) -> None:
        """Profile artifacts cannot escape their declared source root."""
        with tempfile.TemporaryDirectory() as temp:
            profile_path = Path(temp) / "invalid.toml"
            profile_path.write_text(
                """
name = "invalid"
[[program]]
source = "runtime"
path = "../kernel.elf"
argv = ["invalid"]
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfileError, "relative path"):
                load_profile(profile_path)


class ImagePlanTests(unittest.TestCase):
    """Validate artifact resolution and deterministic RAMFS layering."""

    @staticmethod
    def _runtime(root: Path) -> Path:
        runtime = root / "runtime"
        bin_dir = runtime / "bin"
        bin_dir.mkdir(parents=True)
        for name in (
            "mkimage.elf",
            "mkimage.exe",
            "mkramfs.elf",
            "mkramfs.exe",
            "kernel.elf",
            "nanvixd.elf",
            "nanvixd.exe",
            "procd.elf",
        ):
            (bin_dir / name).write_bytes(name.encode())
        return runtime

    def test_prepares_commands_and_ramfs(self) -> None:
        """A profile resolves package artifacts and stages its guest filesystem."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = self._runtime(root)
            packages = root / "packages"
            app = packages / "demo" / "bin" / "demo.elf"
            app.parent.mkdir(parents=True)
            app.write_bytes(b"demo")

            profile_dir = root / "profiles"
            seed = profile_dir / "rootfs"
            seed.mkdir(parents=True)
            (seed / "init").write_bytes(b"echo ready\r\n")
            profile_path = profile_dir / "demo.toml"
            profile_path.write_text(
                """
name = "demo"
ramfs-directories = ["/tmp"]

[[program]]
source = "runtime"
path = "bin/procd.elf"
argv = ["procd"]

[[program]]
source = "package:demo"
path = "bin/demo.elf"
argv = ["demo", "--ready"]
env = { VALUE = "left;right" }

[[ramfs]]
source = "profile"
path = "rootfs"
destination = "/"

[[ramfs]]
source = "package:demo"
path = "bin/demo.elf"
destination = "/bin/demo"
""",
                encoding="utf-8",
            )

            plan = prepare_image(
                load_profile(profile_path),
                profile_path,
                ArtifactRoots(runtime=runtime, packages=packages),
                root / "dist",
                root / "staging",
            )

            self.assertEqual(plan.name, "demo")
            demo_entry = next(
                argument
                for argument in plan.mkimage_command
                if argument.endswith(";demo --ready;VALUE=left\\;right")
            )
            artifact, _, command_line = demo_entry.partition(";")
            self.assertTrue(Path(artifact).samefile(app))
            self.assertEqual(command_line, "demo --ready;VALUE=left\\;right")
            self.assertEqual(
                (root / "staging" / "demo" / "rootfs" / "init").read_text(
                    encoding="utf-8"
                ),
                "echo ready\n",
            )
            self.assertEqual(
                (root / "staging" / "demo" / "rootfs" / "bin" / "demo").read_bytes(),
                b"demo",
            )
            self.assertTrue((root / "staging" / "demo" / "rootfs" / "tmp").is_dir())

    def test_resolves_windows_host_tools(self) -> None:
        """Windows images use PE host tools while retaining ELF guest artifacts."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            bin_dir = runtime / "bin"
            bin_dir.mkdir(parents=True)
            for name in (
                "mkimage.exe",
                "mkramfs.exe",
                "kernel.elf",
                "nanvixd.exe",
                "procd.elf",
            ):
                (bin_dir / name).write_bytes(name.encode())
            profile_path = root / "windows.toml"
            profile_path.write_text(
                """
name = "windows"
[[program]]
source = "runtime"
path = "bin/procd.elf"
argv = ["procd"]
""",
                encoding="utf-8",
            )

            with patch("nanvix_distro.image.HOST_EXECUTABLE_SUFFIX", ".exe"):
                plan = prepare_image(
                    load_profile(profile_path),
                    profile_path,
                    ArtifactRoots(runtime=runtime, packages=root / "packages"),
                    root / "dist",
                    root / "staging",
                )

            self.assertEqual(Path(plan.mkimage_command[0]).name, "mkimage.exe")
            self.assertEqual(plan.copies[-1][1].name, "nanvixd.exe")

    def test_ramfs_conflict_fails(self) -> None:
        """Two layers cannot silently replace the same guest path."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = self._runtime(root)
            packages = root / "packages"
            first = packages / "one" / "value"
            second = packages / "two" / "value"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")

            profile_path = root / "conflict.toml"
            profile_path.write_text(
                """
name = "conflict"
[[program]]
source = "runtime"
path = "bin/procd.elf"
argv = ["procd"]
[[ramfs]]
source = "package:one"
path = "value"
destination = "/value"
[[ramfs]]
source = "package:two"
path = "value"
destination = "/value"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ImageError, "conflict"):
                prepare_image(
                    load_profile(profile_path),
                    profile_path,
                    ArtifactRoots(runtime=runtime, packages=packages),
                    root / "dist",
                    root / "staging",
                )


if __name__ == "__main__":
    unittest.main()
