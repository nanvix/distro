# Run

## Compose a Distribution with Menuconfig

Build the packages once, then open the menu-driven composer:

```sh
python3 z.py build
python3 z.py menuconfig developer
```

The interface follows the familiar menuconfig controls:

- Up/Down or `j`/`k`: move between components
- Space: enable or disable the selected component
- Enter: save the configuration and build the image
- `q` or Escape: cancel

At least one binary must be selected: BusyBox, CPython, or QuickJS. BusyBox
supplies `/init`, `ash`, and the base utilities. A composed image has one boot
workload: BusyBox takes priority, followed by CPython, then QuickJS. Additional
selected interpreters are staged in RAMFS for that workload to launch. From a
BusyBox shell they are available as `python3` and `qjs`; when CPython is the
boot workload, QuickJS is available at `/bin/qjs`.

The command writes reusable inputs and builds the image immediately:

```text
distributions/developer/
├── profile.toml
└── rootfs/
  └── init                  # BusyBox selections only

build/dist/developer/
├── nanvixd.elf|nanvixd.exe  # Linux or Windows host binary
└── bin/
    ├── kernel.elf
    ├── nanvix.initrd
    └── nanvix.ramfs
```

Run `menuconfig` again with the same name to reload and change the selection.
For non-interactive use, pass components explicitly:

```sh
# CPython without BusyBox.
python3 z.py menuconfig developer --include cpython

# BusyBox plus Python and QuickJS.
python3 z.py menuconfig developer --include busybox python quickjs

# QuickJS without BusyBox.
python3 z.py menuconfig developer --include quickjs

# Every component currently offered by the composer.
python3 z.py menuconfig developer --include all
```

`python` and `cpython` are equivalent component names, as are `javascript` and
`quickjs`. Components may be separated by spaces or commas. The composer uses local artifacts
under `build/sysroot/` and `build/deps/`; run `python3 z.py build` first if they do not exist.
The names `busybox`, `python`, and `javascript` are reserved for the built-in
profiles.
`python3 z.py distclean` preserves saved profiles under `distributions/`, but also recursively
deletes ignored and untracked files from submodules and cleans the top-level checkout as described
in [Clean](build.md#clean).

Boot a composed image with KVM on Linux or WHP on Windows:

```sh
python3 z.py run developer
```

Use `python z.py run developer` in PowerShell. The runner selects `nanvixd.elf` and
`/dev/stdout` on Linux or `nanvixd.exe` on Windows, then inherits the terminal for interactive
input and output.

## Named Distributions

### BusyBox

```sh
python3 z.py dist busybox
```

This creates `build/dist/busybox/` and boots BusyBox as `ash /init` after the standard Nanvix
daemons. The profile stages `/init` and `/bin/busybox` in RAMFS.

```sh
python3 z.py run busybox
```

The init script creates the minimal runtime environment, prints
`NANVIX_BUSYBOX_READY`, and enters an interactive shell.

### CPython

```sh
python3 z.py dist python
```

This preserves the CPython environment and prebuilt CPython RAMFS:

- `nanvixd.elf` or `nanvixd.exe` — the host-native Nanvix daemon (VMM)
- `bin/kernel.elf` — the Nanvix kernel
- `bin/nanvix.initrd` — initrd with system daemons + CPython
- `bin/nanvix.ramfs` — filesystem image

```sh
python3 z.py run python
```

### QuickJS

```sh
python3 z.py dist javascript
python3 z.py run javascript
```

## Custom TOML Profiles

Pass a TOML path to create another distribution:

```sh
python3 z.py dist path/to/profile.toml
```

A profile requires a safe `name` and at least one `[[program]]` or `[init]` entry. Programs are
written to the initrd in declaration order; `[init]`, when present, is appended last. Artifact
sources are resolved from `build/sysroot`, `build/deps/<package>`, or the profile directory:

```toml
name = "mini"
kernel-args = ""
ramfs-directories = ["/tmp"]

[[program]]
source = "runtime"
path = "bin/procd.elf"
argv = ["procd"]

[[program]]
source = "runtime"
path = "bin/memd.elf"
argv = ["memd"]

[[program]]
source = "runtime"
path = "bin/vfsd.elf"
argv = ["vfsd"]

[init]
source = "package:busybox"
path = "bin/busybox.elf"
interpreter = "ash"
script = "rootfs/init"
destination = "/init"
env = { HOME = "/", PATH = "/bin:/usr/bin" }

[[ramfs]]
source = "package:busybox"
path = "bin/busybox.elf"
destination = "/bin/busybox"
```

### Top-Level Fields

| Field               | Required    | Behavior                                                             |
| ------------------- | ----------- | -------------------------------------------------------------------- |
| `name`              | Yes         | Output name; starts alphanumerically, then accepts `.`, `_`, and `-` |
| `kernel-args`       | No          | Kernel arguments; defaults to empty and rejects NUL and line breaks  |
| `[[program]]`       | Conditional | Ordered ELF program; at least one program or `[init]` is required    |
| `[init]`            | Conditional | Final interpreter program plus a profile-relative guest script       |
| `ramfs-directories` | No          | Array of absolute guest directories to create                        |
| `[[ramfs]]`         | No          | File or directory layer copied to an absolute guest destination      |
| `[ramfs-image]`     | No          | Prebuilt RAMFS image used instead of staged RAMFS content            |

Each `[[program]]` requires `source`, `path`, and a non-empty `argv` array. Its optional `env`
table maps environment-variable names to values. `source` must be `runtime`, `profile`, or
`package:<name>`; `path` is a relative POSIX path below that source root. Individual argv and
environment tokens must be non-empty and cannot contain whitespace.

The `[init]` table requires `source`, `path`, `interpreter`, and `script`. The first two fields
select the interpreter executable, while `script` is always resolved relative to the profile
directory and copied into RAMFS. `destination` is optional and defaults to `/init`; optional
`args` are appended after that destination in the interpreter argv. `env` may use either an
inline table, as above, or a standard TOML table:

```toml
[init.env]
HOME = "/"
PATH = "/bin:/usr/bin"
```

Additional `[[ramfs]]` entries require `source`, `path`, and `destination`. A file is copied to
the destination, while a directory merges its children there. `ramfs-directories` creates empty
guest directories without placeholder files.

To reuse an already-built filesystem image, select it by source and relative path:

```toml
[ramfs-image]
source = "package:cpython"
path = "cpython-ramfs.img"
```

`[ramfs-image]` is mutually exclusive with `[[ramfs]]`, `ramfs-directories`, and `[init]`, because
`[init]` also stages its script in RAMFS.

Profile validation rejects unknown keys, unsafe names and paths, duplicate `ramfs-directories`,
whitespace inside argv or environment tokens, and malformed Nanvix command lines. Image
preparation rejects missing artifacts and conflicting RAMFS layers instead of replacing an
existing guest path. Guest scripts are copied as data and are never evaluated by the host builder.

Every distribution retains the same output layout:

```text
build/dist/<name>/
├── nanvixd.elf|nanvixd.exe  # Linux/KVM or Windows/WHP
└── bin/
    ├── kernel.elf
    ├── nanvix.initrd
    └── nanvix.ramfs
```
