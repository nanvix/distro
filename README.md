# Nanvix Distribution

Nanvix is a micro-kernel based operating system designed to host applications in a
hypervisor-isolated environment with low overheads.

This repository builds a small, composable Nanvix distribution from source. It
combines a contract-pinned Nanvix SDK, userspace packages, and TOML image
profiles, optionally including a BusyBox environment with a guest `/init` script.

## Key Features

- **Hypervisor Isolation**: Nanvix runs guest applications inside micro-VMs for strong compute
  isolation.
- **Co-Designed Micro-VM**: Nanvix includes a minimal VMM designed specifically for the OS.
- **Micro-Kernel OS**: System services and applications run in user space.
- **Profile-Based Images**: Typed TOML profiles select boot programs, argv/environment, RAMFS
  content, and an optional guest init script.
- **Immutable SDK Toolchain**: All userspace ports build with the digest-pinned SDK image
  recorded in `config/sdk-release.json`.

## Quick Start

Requires an x86_64 Linux host or Windows 11, Git, Docker, Python 3.12+, Rustup with the toolchain
selected by `nanvix/rust-toolchain`, and the Nanvix host build dependencies. Linux uses KVM;
Windows uses Windows Hypervisor Platform (WHP). A hypervisor is required to boot images and run VM
tests, but not to build artifacts.

On every host, initialize the submodules first:

```bash
git submodule update --init --recursive
```

On Ubuntu, including Ubuntu under WSL 2, install the Nanvix host dependencies. This requires
`sudo`:

```bash
cd nanvix
./z setup
rustup show
cd ..
```

On Windows, enable Developer Mode and WHP, restart after enabling WHP, then run the native setup:

```powershell
cd nanvix
.\z.ps1 setup
rustup show
cd ..
```

```bash
# Build Nanvix and all userspace packages.
python3 z.py build

# Open a menuconfig-style selector and build a reusable distribution.
python3 z.py menuconfig developer

# Boot it with KVM on Linux or WHP on Windows.
python3 z.py run developer
```

Use `python` in place of `python3` in PowerShell. For Linux/KVM validation from Windows, run the
Linux commands inside WSL and verify that `/dev/kvm` is readable and writable first. Before
switching the same checkout between native Windows and WSL builds, run `python z.py distclean` in
PowerShell or `python3 z.py distclean` in WSL; the checkout contains shared host-specific staged
artifacts.

The menu requires at least one binary: BusyBox, CPython, or QuickJS. BusyBox
provides an `ash` shell. Without BusyBox, CPython is the boot workload when
selected; otherwise QuickJS boots directly. Additional interpreters are staged
in RAMFS for the boot workload to launch. The command saves reusable files
under `distributions/developer/`.

For scripts and CI, bypass the terminal menu with `--include`:

```bash
python3 z.py menuconfig developer --include python quickjs
```

`python`/`cpython` and `javascript`/`quickjs` are equivalent component names.

Named profiles remain available:

```bash
python3 z.py dist busybox
python3 z.py dist python
python3 z.py dist javascript
```

Pass a TOML path instead of a name to build a custom image:

```bash
python3 z.py dist path/to/my-profile.toml
```

## Documentation

- [doc/build.md](doc/build.md) — Building Nanvix and userspace ports.
- [doc/run.md](doc/run.md) — Creating distributions and running applications.
- [doc/test.md](doc/test.md) — Running tests.
- [doc/packages.md](doc/packages.md) — Supported packages and versions.
- [doc/project-structure.md](doc/project-structure.md) — Project layout.

## Usage Statement

This project is a prototype. As such, we provide no guarantees that it will work and you are
assuming any risks with using the code. We welcome comments and feedback. Please send any questions
or comments to any of the following maintainers of the project:

- [Pedro Henrique Penna](https://github.com/ppenna) - [ppenna@microsoft.com](mailto:ppenna@microsoft.com)

> By sending feedback, you are consenting that it may be used in the further development of this project.

## License

This project is distributed under the [MIT License](LICENSE.txt). Individual package
submodules retain their respective upstream licenses.
