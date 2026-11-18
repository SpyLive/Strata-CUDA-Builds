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

The optional [single-file `strata-updater.py`](strata-updater.py) is a persistent `systemd` service. Every **12 hours** it checks this repository for new Strata builds and changes to its **own script in `main`**. It verifies downloaded packages, waits for the model to unload, updates Engine and Web/API Server, tests startup, rolls back failures, and retains two installed release versions. An initially stopped Strata service is started for verification and stopped again after successful cleanup. Self-updates restart the updater process only, without restarting Strata.

**Minimum requirements:** Linux with `systemd`, Python **3.12+**, `sudo`, `curl`, access to GitHub, and sufficient free disk space. The non-root user installing the updater must be the same as `User=` in the existing system `strata.service`. Its home directory must already contain **real writable directories**:

```text
~/strata/
├── strata-sm120/   # Active CUDA engine and libraries
└── server/         # Active Python API and Web UI
```

The existing `strata.service` must use those stable paths for its engine, vision executable, CUDA libraries, and working directory. By default the local Strata API is at `http://127.0.0.1:8090`. Installation does not change Strata configuration or models.

**Install in one command** as the Strata service user (do not prefix this command with `sudo`):

```bash
tmp=$(mktemp) && curl -fsSL https://raw.githubusercontent.com/SpyLive/Strata-CUDA-Builds/main/strata-updater.py -o "$tmp" && python3 "$tmp" --install; code=$?; rm -f "$tmp"; (exit "$code")
```

**Update the updater manually and restart it on the new version:**

```bash
python3 "$HOME/.local/bin/strata-updater.py" --update
```

With updater **v1.0.2 or newer**, this command requests an immediate self-update from the GitHub `main` branch. The running service applies it at a safe point and restarts its own Python process. **Automatic self-updates** are checked every 12 hours, including during long waits for model unloading. GitHub does not need inbound access to the server. A manual update requested while Strata files are being replaced is deferred until that operation is complete.

**One-time upgrade from updater v1.0.1 (without `--update`):** repeat the install command above and then, when no Strata update is in progress, run `sudo systemctl restart strata-updater.service`. Future updater upgrades use `--update` directly.

**Repair a service installed by v1.0.2 that fails with `WorkingDirectory= path is not absolute`:** rerun the install command above. Version **1.0.3+** regenerates a valid systemd unit, validates it before installation, and confirms that the updater service started. `--update` cannot repair this case while the updater service is stopped.

**Monitor progress, check status, or request a Strata-release check:**

```bash
journalctl -u strata-updater.service -f
python3 "$HOME/.local/bin/strata-updater.py" --status
python3 "$HOME/.local/bin/strata-updater.py" --check-now
```

**Uninstall in one command:**

```bash
python3 "$HOME/.local/bin/strata-updater.py" --uninstall
```

Uninstall removes the updater service and its restricted `sudo` permission, leaving Strata binaries, release archives, models, and saved state intact.

## Compatibility and security

The resulting binaries are **unsigned unofficial builds**, not part of the official Strata signed update feed. GitHub-hosted runners do not perform GPU execution tests. Validate the engine and vision features on a supported GPU before production use.

Strata and third-party components retain their respective upstream licenses.
