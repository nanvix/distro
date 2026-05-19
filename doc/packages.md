# Packages

Nanvix ships with the following userspace packages, cross-compiled for x86 by the immutable
`ghcr.io/nanvix/nanvix-sdk-c-clang` image pinned in `config/sdk-release.json`.

## Libraries

| Package | Version | Description                |
| ------- | ------- | -------------------------- |
| zlib    | 1.3.1   | Compression library        |
| bzip2   | 1.0.8   | Block-sorting compressor   |
| xz      | 5.2.5   | LZMA compression           |
| OpenSSL | 3.5.0   | TLS and cryptography       |
| libffi  | 3.4.6   | Foreign-function interface |
| libxml2 | 2.12.9  | XML parser                 |
| libxslt | 1.1.42  | XSLT processor             |
| lxml    | 5.3.0   | Python XML/XSLT bindings   |

## Runtimes and Applications

| Package | Version    | Description                       |
| ------- | ---------- | --------------------------------- |
| CPython | 3.12.3     | Python interpreter                |
| SQLite  | 3.49.0     | Embedded SQL database             |
| QuickJS | 2025-09-13 | JavaScript engine                 |
| BusyBox | 1.36.1     | Shell and compact POSIX utilities |

## Build Order

Ports are built in a deterministic dependency order. Independent packages may appear in any
topologically valid position; all dependencies precede their consumers:

CPython consumes zlib, SQLite, OpenSSL, bzip2, libffi, libxml2, libxslt, lxml, and xz.
SQLite consumes zlib; libxml2 consumes zlib; libxslt consumes libxml2 and zlib; and lxml
consumes zlib, libxml2, and libxslt. BusyBox and QuickJS are independent applications.

Every package is pinned to the SDK release coordinate in `config/sdk-release.json`; the exact
gitlinks and port lockfiles provide the authoritative revisions.
