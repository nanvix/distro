# Project Structure

```
z.py                         Top-level build, test, image, run, and upgrade orchestrator
config/sdk-release.json      Authoritative immutable SDK/runtime coordinate
nanvix_distro/               Typed SDK contract and image-profile helpers
profiles/                    Named TOML distribution profiles
profiles/rootfs/             Guest files copied by named profiles
distributions/<name>/        Reusable menuconfig-generated profiles
nanvix/                      Nanvix kernel, daemons, and host tools
usr/bin/                     Binary and mixed userspace package submodules
usr/lib/                     Library and zutils submodules
tests/                       Top-level Python regression tests
build/sysroot/               Locally built Nanvix runtime artifacts (generated)
build/deps/<package>/        Canonical package exports (generated)
build/staging/<profile>/     RAMFS assembly trees (generated)
build/dist/<profile>/        Self-contained distribution images (generated)
~/.cache/nanvix-distro/      WSL Cargo targets for Windows-mounted checkouts (generated)
```

The SDK source is not vendored. Userspace builds consume the published OCI image named by
`config/sdk-release.json`; each package repeats that coordinate in its committed manifest and
lockfile.
