# Strata CUDA 13.2 / sm_120 builds

Unofficial automated Linux x86-64 builds of [Strata](https://github.com/Niko1221/Strata) for NVIDIA GPUs with compute capability `sm_120`.

## Build specification

- **Toolkit:** CUDA 13.2, GPU architecture sm_120 + compute_120
- **Driver:** NVIDIA 595.71.05 or newer
- **Build system:** Ubuntu 24.04 / GCC 13, upstream-compatible CMake and Ninja
- **Components:** Strata Engine, strata-vision, matching Python API and Web interface
- **Supported upstream tags:** v0.2.14, v0.2.15 and all subsequent versions. Tags older than v0.2.14 are not supported.
- **Source layout detection:** v0.2.14–v0.2.15 use root-level packaging; v0.2.16 uses engine/, terminal/ and tools/packaging/. Unknown future layouts fail explicitly.

The official upstream sources are compiled without permanent source changes (other than the locally patched minimum-driver field in the packaging recipe). These builds are unofficial and unsigned.

## Release files

The new split-library release format has **five assets**, for example:

```text
strata-engine-v0.2.16-cuda13.2-sm120-linux-x86_64.tar.gz
├── strata-v0216-sm120/
│   ├── strata
│   ├── strata-vision
│   ├── engine-component.json
│   ├── BUILD.json
│   └── SYSTEM-LIBS.txt
└── server-v0216/
    ├── serve/
    ├── strata_control/
    ├── tools/
    ├── requirements.txt
    ├── strata.py
    └── SOURCE-SHA.txt

strata-cuda-libs-cuda13.2-linux-x86_64.tar.gz
└── libs/
    ├── libcublasLt.so.13
    ├── libcublas.so.13
    ├── libcudart.so.13
    └── CUDA-LIBS.json

CUDA-LIBS.json       # byte-for-byte identical to libs/CUDA-LIBS.json
SHA256SUMS.txt       # archive and standalone manifest SHA-256
BUILD-SOURCE.txt     # exact upstream source and build details
```

The Engine archive **does not** contain the three CUDA libraries. Their hashes and sizes are recorded only in the separate CUDA-LIBS.json. Upstream Strata version and internal Engine version differ: v0.2.16 contains Engine 0.1.62.

The v0.2.15 and v0.2.16 split-format Releases passed the GitHub Actions build, manifest and archive checks.

## Build automation

[GitHub Actions](../../actions/workflows/build-strata.yml) checks the latest stable upstream release every six hours and publishes to [Releases](../../releases). Only three most recent successful custom Releases are retained.

To compile manually, open **Actions → Strata CUDA 13.2 / sm_120 → Run workflow**:

- **upstream_tag:** specific upstream tag, or empty for the newest stable release
- **rebuild_existing:** true fully recompiles and replaces the assets of an existing release; false skips existing releases

The updater only accepts Releases containing the full split format. Earlier unsplit archives are not treated as compatible; there is no legacy fallback.

## Automatic updater v1.1.0

[strata-updater.py](strata-updater.py) is a single-file, self-updating systemd service running as the Strata service user. Every **12 hours** it checks GitHub for new Releases and an updated version of its own script. An update is prepared and verified first, then waits for the model to unload and for in-flight requests to finish. The updater stops Strata only when safe, installs Engine and Server, verifies the HTTP service, and rolls back failed installations.

**Separate CUDA library policy:**

1. Download the tiny standalone CUDA-LIBS.json and verify it against SHA256SUMS.txt and the GitHub asset digest.
2. Compare the published JSON byte-for-byte with ~/strata/libs/CUDA-LIBS.json.
3. If the local JSON is missing and three existing CUDA libraries are present, compute their SHA-256 **once**, create the canonical JSON atomically, and then compare.
4. When JSON matches, **do not rehash local libraries and do not download the CUDA archive**. Reuse the existing ~/strata/libs/ directory.
5. If JSON differs or libraries are absent, download the separate CUDA archive, verify each newly downloaded library's SHA-256 and size, and stage it.
6. Replace ~/strata/libs/ only when necessary. For a failed installation, restore the old CUDA library directory together with Engine and Server. When libraries match, they are never replaced.

Engine, Server and CUDA library files are updated separately from model weights and model-selector profile files. The updater retains the installed current and previous Engine/Server release directories. If Strata was stopped before verification, it is returned to the stopped state afterwards.

### System configuration

Linux with systemd, Python 3.12+, sudo and GitHub access is required. The user who installs the updater must match User= in strata.service. Active directories:

```text
~/strata/
├── strata-sm120/   # Engine and Vision binaries
├── server/         # Python API and Web UI
└── libs/           # libcublasLt, libcublas, libcudart, CUDA-LIBS.json
```

The libs/ directory may already exist and contain the three libraries. If the metadata file is absent, the directory must be writable by the updater user to create it.

Add the following to each run-profile JSON. Replace `/home/USERNAME/strata` with the absolute path to the installation directory:

```json
"lib_dirs": [
  "/home/USERNAME/strata/libs",
  "/home/USERNAME/strata/strata-sm120"
]
```

The Strata server passes these directories to both Engine and Vision through `LD_LIBRARY_PATH`. Keep `libs/` first. If a profile is selected through a symbolic link, edit its resolved target instead of replacing the link.

### Install, update and monitor

Install once as the Strata service user **without sudo**:

```bash
tmp=$(mktemp) && curl -fsSL https://raw.githubusercontent.com/SpyLive/Strata-CUDA-Builds/main/strata-updater.py -o "$tmp" && python3 "$tmp" --install; code=$?; rm -f "$tmp"; (exit "$code")
```

Upgrade an already installed updater (version 1.0.2 or newer) to the latest file in GitHub main:

```bash
python3 "$HOME/.local/bin/strata-updater.py" --update
```

This requests a safe self-update of the updater process, not a forced Strata restart. A pending model update still waits for the model to unload.

Inspect or request an immediate release check:

```bash
systemctl status strata-updater.service --no-pager
journalctl -u strata-updater.service -f
python3 "$HOME/.local/bin/strata-updater.py" --version
python3 "$HOME/.local/bin/strata-updater.py" --status
python3 "$HOME/.local/bin/strata-updater.py" --check-now
```

If a unit installed with updater 1.0.2 fails because WorkingDirectory is not absolute, rerun the install command above; this was fixed in 1.0.3.

Uninstall only the updater (leave Strata binaries, models and saved state intact):

```bash
python3 "$HOME/.local/bin/strata-updater.py" --uninstall
```

## Integrity and runtime limitations

Downloaded archives are verified against SHA256SUMS.txt and GitHub asset SHA-256 records. The separate CUDA archive's library files are hash-verified before activation. Archive unpacking rejects unsafe paths, symlinks and special files.

**Successful GitHub Actions compilation is not a real-GPU inference test.** Verify both Strata Engine and strata-vision on your target NVIDIA GPU after installation. GitHub-hosted runners do not perform live GPU tests.
