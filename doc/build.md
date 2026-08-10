# Build

## Prerequisites

- Linux x86_64 or Windows 11 x86_64 host
- Git with all repository submodules initialized
- Docker (for the digest-pinned Nanvix SDK)
- Python 3.12+
- Rustup with the toolchain selected by `nanvix/rust-toolchain`
- GNU Make and the host-specific Nanvix build dependencies

KVM access (`/dev/kvm`) is required to boot on Linux. Windows requires Windows Hypervisor Platform
(WHP). Neither hypervisor is required to create build artifacts.

On a fresh checkout, initialize nested submodules before running the orchestrator:

```sh
git submodule update --init --recursive
```

On Ubuntu, the Nanvix setup command installs the host build dependencies and configures its
development environment. It requires `sudo`; Rustup must already be installed:

```sh
cd nanvix
./z setup
rustup show
cd ..
```

On Windows 11, enable Developer Mode and WHP first. Restart after enabling WHP, then run the native
setup from PowerShell. It checks Visual Studio Build Tools, LLVM/Clang, GNU Make, Python, Rust, and
the hypervisor:

```powershell
cd nanvix
.\z.ps1 setup
rustup show
cd ..
```

Ubuntu under WSL 2 follows the Linux instructions. Before using it for VM validation, verify KVM
access:

```bash
test -r /dev/kvm && test -w /dev/kvm
```

For a checkout under `/mnt/<drive>`, the orchestrator automatically places Nanvix Cargo outputs
under `~/.cache/nanvix-distro/` on WSL's ext4 filesystem. This avoids compiler-cache permission
failures and very slow filesystem-image generation on the Windows-mounted `9p` filesystem.
`distclean` removes this external target directory together with `build/`.

When one checkout is shared by native Windows and WSL, run `python z.py distclean` (Windows) or
`python3 z.py distclean` (WSL) before switching hosts. This removes shared host-specific files from
Nanvix `bin/` and `lib/` plus the distribution build tree; WSL cleanup also removes its external
Cargo target cache.

## SDK and Runtime Compatibility

`config/sdk-release.json` is the build's source of truth. It pins the immutable SDK image,
Nanvix runtime commit, target ABI, and sysroot checksum. Before compiling, the build verifies the
Nanvix gitlink; each port manifest's Nanvix version and SDK image coordinate; and each port
lockfile's SDK image, Nanvix, and sysroot fields.

The SDK supplies the compiler, headers, startup objects, linker scripts, and C/C++ runtime.
`build/sysroot/` contains only the locally built Nanvix runtime and host tools.

## Build All Components

```sh
python3 z.py build
```

In PowerShell, use `python z.py build`. Nanvix builds its host utilities natively: Linux produces
`.elf` host binaries with the KVM backend, while Windows produces `.exe` host binaries with WHP.
Guest programs and the kernel remain ELF files on both hosts. Userspace ports always build in the
digest-pinned Linux SDK container.

This builds:

1. Nanvix core (kernel, daemons, host tools) with `LOG_LEVEL=error`
2. All userspace ports in dependency order

The current package order is:

```text
zlib, bzip2, xz, openssl, libffi, busybox, quickjs,
sqlite, libxml2, libxslt, lxml, cpython
```

Each port runs its own SDK-era zutils build hook. Dependencies already built by the distro are
exported to `build/deps/<name>/` and consumed through zutils offline overlays. Docker must be
available; the build does not silently skip userspace packages.

## Verbose Output

```sh
python3 z.py --verbose build
```

## Dry Run

```sh
python3 z.py --dry-run build
```

This validates the committed SDK coordinate and prints the Nanvix and port commands without
building artifacts.

`--verbose` and `--dry-run` are global options and must precede the subcommand.

## Upgrade the Distro

Upgrade the distro, including its SDK and every module, to the latest completed release set:

```sh
python3 z.py --dry-run upgrade
python3 z.py upgrade
```

Set `SDK_RELEASE_TAG` to an exact completed SDK release tag and pass it with `--sdk-version`:

```sh
python3 z.py upgrade --sdk-version "$SDK_RELEASE_TAG"
```

Without `--sdk-version`, the command falls back to the latest completed release. Upgrade preflight
resolves every target before changing a checkout. It uses the SDK release's
`sdk-release.json` asset as the readiness signal, pins Nanvix to the contract's exact commit, and
selects only package tags ending in the matching `-nanvix-<version>-sdk.<revision>` coordinate.
Independent development-branch tips are never mixed into the release set.

Preflight rejects a release commit that is an ancestor of the repository's current submodule pin,
because selecting it would silently discard newer work. Publish a release containing the current
pin, or opt into an intentional rollback explicitly:

```sh
python3 z.py upgrade --sdk-version "$SDK_RELEASE_TAG" --allow-downgrade
```

Upgrade dry-run does not change tracked files or checked-out commits, but preflight still requires
GitHub access, may initialize missing top-level submodules, and fetches tags and refs in every
top-level submodule.

The [Distro Autoupgrade workflow](../.github/workflows/distro-autoupgrade.yml) runs nightly and can be
dispatched manually. It opens or refreshes a pull request from the dedicated
`automation/distro-autoupgrade` branch containing the SDK contract and matching gitlink updates.
The workflow dispatches the Distro Test workflow for the pull request head, waits for that
exact run to pass, and merges the validated head commit. When the repository already uses the
latest release set, it makes no changes.

## Clean

Reset generated workspace state:

```sh
python3 z.py distclean
```

This removes `build/`, recursively deletes ignored and untracked files from every submodule, and
cleans ignored and untracked files from the top-level checkout. At the top level it preserves
`.venv/`, `*.py`, `doc/`, and `distributions/`. Commit or otherwise preserve untracked work before
running it.
