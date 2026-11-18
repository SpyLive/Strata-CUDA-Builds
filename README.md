# Strata CUDA 13.2 / sm_120 builds

Unofficial automated Linux x86-64 builds of [Strata](https://github.com/Niko1221/Strata) for NVIDIA Blackwell GPUs (`sm_120`).

## Build specification

- **CUDA Toolkit:** 13.2
- **GPU architectures:** `sm_120` and `compute_120`
- **Build:** Ubuntu 24.04, GCC 13, CMake 4.2.3, Ninja 1.13.2, Release
- **Components:** `strata`, `strata-vision`, matching Python API and Web interface
- **Minimum NVIDIA driver:** 595.71.05
- **Packaging:** required CUDA userspace libraries are included as regular files; standard Linux OS libraries and the NVIDIA driver remain system dependencies
- **Integrity:** engine manifest and archive SHA-256

## Release archive

Each GitHub Release contains a single Linux `.tar.gz` archive with two top-level directories. Example for upstream `v0.2.14`:

```text
strata-engine-v0.2.14-cuda13.2-sm120-linux-x86_64.tar.gz
├── strata-v0214-sm120/
│   ├── strata
│   ├── strata-vision
│   ├── libcudart.so.13
│   ├── libcublas.so.13
│   ├── libcublasLt.so.13
│   ├── engine-component.json
│   ├── BUILD.json
│   └── SYSTEM-LIBS.txt
└── server-v0214/
    ├── serve/
    ├── strata_control/
    ├── tools/
    ├── requirements.txt
    ├── strata.py
    └── SOURCE-SHA.txt
```

Both components come from the same upstream source tag. Directory suffixes are derived from the tag (e.g. `v0.2.15` → `v0215`). The engine's own internal version may differ from the upstream application version.

## Automation

[GitHub Actions](../../actions) checks the latest stable upstream release **every six hours**. When a new version appears, it compiles and verifies the package, then publishes it in [Releases](../../releases).

Already published versions are skipped. Only the **three latest successfully published custom releases** are retained; failed builds do not remove existing releases.

Manual builds: **Actions → Strata CUDA 13.2 / sm_120 → Run workflow**.

- `upstream_tag`: optional upstream release tag; blank selects the newest stable release.
- `rebuild_existing`: optional, defaults to `false`. Set `true` to recompile and replace the files of an existing release.

## Compatibility and security

The resulting binaries are **unsigned unofficial builds**, not part of the official Strata signed update feed. GitHub-hosted runners do not perform GPU execution tests. Validate the engine and vision features on a supported GPU before production use.

Strata and third-party components retain their respective upstream licenses.
