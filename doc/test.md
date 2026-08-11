# Test

Run the top-level regression tests:

```sh
python3 -m unittest discover -v
```

Use `python -m unittest discover -v` on Windows.

The test and quality-tool dependencies are not installed by the project. Install them in a virtual
environment, then run the same checks as CI:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install black pyright tomli-w
```

Configure the repository's pre-commit hook once per checkout. The hook runs Black and Pyright
before each commit:

```sh
git config --local core.hooksPath .githooks
```

Run the same checks manually with:

```sh
black --target-version py312 --check z.py nanvix_distro tests
pyright
```

Run the Nanvix runtime test target:

```sh
python3 z.py test
```

This rebuilds Nanvix as needed and runs its unit, kernel, VM smoke, integration, and POSIX test
suites. Linux VM tests require KVM access through `/dev/kvm`; native Windows tests use WHP. The
top-level orchestrator does not run userspace package test suites.

To validate Linux/KVM from a Windows workstation, run the Linux commands under WSL 2:

```powershell
wsl.exe --distribution Ubuntu --exec bash -lc `
  'cd /mnt/c/path/to/nanvix-distro && test -r /dev/kvm && test -w /dev/kvm && python3 z.py test'
```

The GitHub-hosted Linux/KVM CI job additionally:

1. runs Black, Pyright, and the top-level Python regression tests;
2. builds Nanvix and every userspace package through the pinned SDK;
3. creates the BusyBox, CPython, QuickJS, and all-component menuconfig images;
4. boots BusyBox and requires the `NANVIX_BUSYBOX_READY` marker;
5. boots the all-component image and exercises both CPython and QuickJS; and
6. runs the Nanvix test target with `python3 z.py --verbose test`.

The GitHub-hosted Windows/WHP job runs the same top-level tooling checks, then builds, tests, and
installs the native Nanvix runtime from a short drive mapping. Userspace package outputs are guest
ELF files, so the job downloads the release archives selected by the exact BusyBox, QuickJS, and
CPython submodule tags and verifies their GitHub-published SHA-256 digests instead of requiring a
Linux Docker daemon on the Windows runner. It composes the three standalone images and the
all-component image with the native `.exe` host tools, boots BusyBox through
`python z.py --verbose run busybox`, and requires the
`NANVIX_BUSYBOX_READY` marker before it succeeds. Pull requests use a debug Nanvix runtime;
canonical `main` pushes use a release runtime and stage BusyBox, QuickJS, CPython, and
all-component WHP ZIP archives alongside the Linux/KVM release assets.

Run the BusyBox smoke test used by CI manually after `python3 z.py build`:

```bash
python3 z.py dist busybox
set -o pipefail
printf 'exit\n' | timeout 120s python3 z.py run busybox 2>&1 | tee busybox-smoke.log
grep -q "NANVIX_BUSYBOX_READY" busybox-smoke.log
```

Run the composed-image smoke test used by CI:

```bash
python3 z.py menuconfig ci-composed --include all
set -o pipefail
{
  echo "python3 -c 'print(\"PYTHON_COMPONENT_READY\")'"
  echo "qjs -e 'console.log(\"JAVASCRIPT_COMPONENT_READY\")'"
  echo "exit"
} | timeout 180s python3 z.py run ci-composed 2>&1 | tee composed-smoke.log
grep -q "PYTHON_COMPONENT_READY" composed-smoke.log
grep -q "JAVASCRIPT_COMPONENT_READY" composed-smoke.log
```

On Windows, the equivalent BusyBox smoke test is:

```powershell
python z.py dist busybox
'exit' | python z.py run busybox 2>&1 | Tee-Object busybox-smoke.log
if (-not (Select-String -Quiet 'NANVIX_BUSYBOX_READY' busybox-smoke.log)) {
  throw 'WHP smoke marker not found'
}
```
