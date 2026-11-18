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

## Automatic server updater

The optional [single-file updater](strata-updater.py) runs continuously under `systemd` as **the Linux user who installs it** (not a hard-coded account). It checks this repository's releases every **12 hours**, verifies SHA-256, installs matching Engine and Web/API files, and retains the current and previous release directories. When Strata is running, it waits for the model to unload, checking every minute. After updating, it checks server startup and rolls back on failure. A previously stopped Strata service is started for verification and stopped again after cleanup.

**Minimum requirements:** Linux with `systemd`, Python **3.12+**, `sudo`, `curl`, network access to GitHub, and sufficient free disk space for the download and temporary copies. Install as a regular user with sudo privileges **whose account is also configured as `User=` in the existing system `strata.service`**. Installation grants only narrowly scoped permission to start and stop `strata.service`; uninstall removes that permission.

Before installation, the following **real, writable directories** must already exist under the installing user's home:

```text
~/strata/
├── strata-sm120/  # Active engine binaries and CUDA libraries
└── server/        # Active Python API and Web interface
```

The existing `strata.service` must already start successfully with its working directory set to `~/strata/server`. Its model configuration should use `~/strata/strata-sm120/strata` for the engine, `~/strata/strata-sm120/strata-vision` for vision, and `~/strata/strata-sm120` for library lookup. Model data and external config files are not changed by the updater.

**Install with one command** (run as the Strata service user, **without** `sudo` in front):

```bash
tmp=$(mktemp) && curl -fsSL https://raw.githubusercontent.com/SpyLive/Strata-CUDA-Builds/main/strata-updater.py -o "$tmp" && python3 "$tmp" --install; code=$?; rm -f "$tmp"; (exit "$code")
```

**Uninstall with one command:**

```bash
python3 "$HOME/.local/bin/strata-updater.py" --uninstall
```

The uninstall command removes only the updater, its `systemd` unit and narrowly scoped service-control permission. It does not remove Strata, models, downloaded release folders or saved update status.

Optional commands:

```bash
python3 "$HOME/.local/bin/strata-updater.py" --status
python3 "$HOME/.local/bin/strata-updater.py" --check-now
journalctl -u strata-updater.service -f
```

The default local API is `http://127.0.0.1:8090`. To use another port, pass `--api-url http://127.0.0.1:PORT` when installing. No separate updater configuration file is required.

## Compatibility and security

The resulting binaries are **unsigned unofficial builds**, not part of the official Strata signed update feed. GitHub-hosted runners do not perform GPU execution tests. Validate the engine and vision features on a supported GPU before production use.

Strata and third-party components retain their respective upstream licenses.
