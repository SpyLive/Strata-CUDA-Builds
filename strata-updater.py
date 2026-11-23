#!/usr/bin/env python3
"""Self-installing, unprivileged systemd updater for Strata CUDA sm_120 releases.

Distributed as one Python file. --install generates the systemd unit and a narrowly
scoped sudoers rule so the runtime process can control strata.service as its owner.
"""

import argparse
import base64
import datetime as dt
import hashlib
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import grp
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

VERSION = "1.1.1"
REPO = "SpyLive/Strata-CUDA-Builds"
PREFIX = "strata-sm120-cuda13.2-"
SERVICE = "strata.service"
UPDATER = "strata-updater.service"
INTERVAL = 3 * 60 * 60
POLL = 60
HEALTH_TIMEOUT = 90
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_EXTRACT_BYTES = 4 * 1024**3
MAX_MEMBERS = 25000
MAX_SELF_SOURCE_BYTES = 512 * 1024
TAG_RE = re.compile(r"^strata-sm120-cuda13\.2-v(\d+\.\d+\.\d+(?:\.\d+)?)$")
ENGINE_RE = re.compile(r"^strata-v(\d+)-sm120$")
SERVER_RE = re.compile(r"^server-v(\d+)$")
CUDA_ARCHIVE = "strata-cuda-libs-cuda13.2-linux-x86_64.tar.gz"
CUDA_JSON = "CUDA-LIBS.json"
CUDA_LIBS = ("libcublasLt.so.13", "libcublas.so.13", "libcudart.so.13")
MAX_CUDA_JSON = 16384
WAKE = threading.Event()
SELF_UPDATE_REQUESTED = threading.Event()


def log(message):
    print(f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} {message}", flush=True)


def home():
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def state_file():
    return home() / ".local/state/strata-updater/state.json"


def read_state():
    p = state_file()
    if not p.exists():
        return {}
    obj = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise RuntimeError("Updater state must be a JSON object")
    return obj


def save_state(state):
    p = state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run_command(args, *, timeout=150, check=True):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}: {result.stderr.strip()}")
    return result


def service_state(systemctl):
    result = run_command([systemctl, "show", "-p", "ActiveState", "--value", SERVICE])
    return result.stdout.strip()


def control(systemctl, verb):
    if verb not in {"stop", "start"}:
        raise ValueError("Invalid service action")
    run_command(["sudo", "-n", systemctl, verb, SERVICE], timeout=150)


def request(url, timeout=25):
    headers = {"User-Agent": f"strata-updater/{VERSION}", "Accept": "application/vnd.github+json"}
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)



def fetch_updater_source():
    """Retrieve updater source from GitHub main with its Git blob checksum."""
    url = f"https://api.github.com/repos/{REPO}/contents/strata-updater.py?ref=main"
    with request(url, timeout=35) as response:
        info = json.loads(response.read(MAX_SELF_SOURCE_BYTES * 2))
    encoded = info.get("content")
    if info.get("encoding") != "base64" or not isinstance(encoded, str):
        raise RuntimeError("Unexpected GitHub source response")
    payload = base64.b64decode("".join(encoded.split()), validate=True)
    if not payload or len(payload) > MAX_SELF_SOURCE_BYTES:
        raise RuntimeError("Updater source empty or too large")
    blob_sha = hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()
    if blob_sha != info.get("sha"):
        raise RuntimeError("Updater source checksum does not match GitHub metadata")
    code = payload.decode("utf-8")
    if not code.startswith("#!/usr/bin/env python3"):
        raise RuntimeError("Invalid updater source header")
    compile(code, "strata-updater.py", "exec")
    match = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"$', code, re.MULTILINE)
    if not match:
        raise RuntimeError("Downloaded updater does not declare its version")
    version = match.group(1)
    if tuple(map(int, version.split("."))) < tuple(map(int, VERSION.split("."))):
        raise RuntimeError("Refusing updater downgrade")
    return payload, version


def write_updater_atomically(path, payload, version):
    fd, name = tempfile.mkstemp(prefix=".strata-updater-upgrade-", dir=path.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        staging.chmod(0o755)
        check = run_command([sys.executable, str(staging), "--version"], timeout=20)
        if check.stdout.strip() != version:
            raise RuntimeError("New updater failed startup test")
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def perform_self_update(api_url):
    """At a safe point replace the script and exec into it, preserving systemd MainPID."""
    path = home() / ".local/bin/strata-updater.py"
    if not path.is_file() or path.resolve() != Path(__file__).resolve():
        raise RuntimeError("Self-update requires the installed updater")
    payload, version = fetch_updater_source()
    previous = path.read_bytes()
    if payload == previous:
        return False
    log(f"[SELF-UPDATE] New version in GitHub: {VERSION} -> {version}")
    write_updater_atomically(path, payload, version)
    log(f"[SELF-UPDATE] Installed {version}; restarting updater process")
    try:
        os.execv(sys.executable, [sys.executable, str(path), "--run", "--api-url", api_url])
    except OSError as exc:
        write_updater_atomically(path, previous, VERSION)
        raise RuntimeError(f"Failed restarting updater; restored previous version: {exc}") from exc



def releases():
    url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
    with request(url) as response:
        data = json.load(response)
    candidates = []
    for release in data:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = release.get("tag_name", "")
        match = TAG_RE.fullmatch(tag)
        if not match:
            continue
        version = tuple(map(int, match.group(1).split(".")))
        if version < (0, 2, 14):
            continue
        core_name = f"strata-engine-{tag[len(PREFIX):]}-cuda13.2-sm120-linux-x86_64.tar.gz"
        assets = {a.get("name"): a for a in release.get("assets", []) if isinstance(a, dict)}
        core, cuda = assets.get(core_name), assets.get(CUDA_ARCHIVE)
        meta, checks = assets.get(CUDA_JSON), assets.get("SHA256SUMS.txt")
        if all(a and a.get("browser_download_url") for a in (core, cuda, meta, checks)):
            candidates.append((version, release, core, cuda, meta, checks))
    if not candidates:
        raise RuntimeError("No compatible release with two archives, CUDA-LIBS.json and SHA256SUMS.txt")
    return max(candidates, key=lambda x: x[0])

def asset_digest(asset):
    digest = asset.get("digest", "")
    if not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
        raise RuntimeError(f"Missing SHA-256 digest for release asset {asset.get('name')}")
    return digest.split(":", 1)[1].lower()


def read_checksums(asset):
    with request(asset["browser_download_url"], timeout=45) as response:
        content = response.read(1_000_001)
    if len(content) > 1_000_000:
        raise RuntimeError("Checksum file is unexpectedly large")
    if hashlib.sha256(content).hexdigest() != asset_digest(asset):
        raise RuntimeError("Checksum file does not match GitHub release digest")
    return content.decode("utf-8")


def expected_checksum(text, filename):
    result = None
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match and match.group(2).removeprefix("./") == filename:
            if result is not None:
                raise RuntimeError("Duplicate archive checksum entry")
            result = match.group(1).lower()
    if result is None:
        raise RuntimeError("Archive not listed in SHA256SUMS.txt")
    return result


def cuda_json_bytes(info):
    return (json.dumps(info, sort_keys=True, indent=2) + "\n").encode("utf-8")


def validate_cuda_json(payload):
    try:
        info = json.loads(payload)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid CUDA-LIBS.json: {exc}") from exc
    if (not isinstance(info, dict) or set(info) != {"schema_version", "cuda", "libraries"}
            or info["schema_version"] != 1 or info["cuda"] != "13.2"):
        raise RuntimeError("Unexpected CUDA-LIBS.json schema")
    entries = info["libraries"]
    if not isinstance(entries, dict) or set(entries) != set(CUDA_LIBS):
        raise RuntimeError("CUDA-LIBS.json must contain exactly three libraries")
    for name in CUDA_LIBS:
        entry = entries[name]
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size"}
                or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or type(entry["size"]) is not int or entry["size"] <= 0):
            raise RuntimeError(f"Invalid CUDA library record: {name}")
    if cuda_json_bytes(info) != payload:
        raise RuntimeError("Noncanonical CUDA-LIBS.json")
    return info


def read_cuda_asset(asset, checks):
    expected = expected_checksum(checks, CUDA_JSON)
    if expected != asset_digest(asset):
        raise RuntimeError("CUDA-LIBS.json checksum disagrees with GitHub digest")
    with request(asset["browser_download_url"], timeout=45) as response:
        payload = response.read(MAX_CUDA_JSON + 1)
    if len(payload) > MAX_CUDA_JSON or hashlib.sha256(payload).hexdigest() != expected:
        raise RuntimeError("CUDA-LIBS.json download failed verification")
    return payload, validate_cuda_json(payload)


def local_cuda_manifest(root):
    """Compare existing JSON; hash large libraries only if metadata is missing."""
    folder = root / "libs"
    if not folder.is_dir() or folder.is_symlink():
        return None
    manifest = folder / CUDA_JSON
    if manifest.is_symlink():
        raise RuntimeError("Refusing local CUDA-LIBS.json symlink")
    if manifest.exists():
        if not manifest.is_file():
            raise RuntimeError("Local CUDA-LIBS.json is not a file")
        payload = manifest.read_bytes()
        if len(payload) > MAX_CUDA_JSON:
            raise RuntimeError("Local CUDA-LIBS.json exceeds configured size limit")
        log("[LIBS] Comparing existing CUDA-LIBS.json with published manifest; .so files are not rehashed")
        return payload
    entries = {}
    for name in CUDA_LIBS:
        file = folder / name
        if not file.is_file() or file.is_symlink():
            return None
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        entries[name] = {"sha256": digest.hexdigest(), "size": file.stat().st_size}
    content = cuda_json_bytes({"schema_version": 1, "cuda": "13.2", "libraries": entries})
    fd, path = tempfile.mkstemp(prefix=".cuda-meta-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
        os.replace(path, manifest)
    finally:
        if os.path.exists(path):
            os.unlink(path)
    log("[LIBS] Created local CUDA-LIBS.json with one-time SHA-256 values")
    return content


def same_cuda_libraries(root, published):
    if local_cuda_manifest(root) != published:
        return False
    folder = root / "libs"
    return all((folder / name).is_file() and not (folder / name).is_symlink()
               for name in CUDA_LIBS)


def stage_cuda_archive(archive, root, published, parsed):
    """Unpack and hash only newly downloaded CUDA libraries."""
    target = root / ".strata-updater-new-libs"
    if target.is_symlink():
        raise RuntimeError("Refusing symlink in CUDA staging directory")
    if target.exists():
        shutil.rmtree(target)
    temp = Path(tempfile.mkdtemp(prefix=".cuda-unpack-", dir=root))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            names = [m.name for m in members]
            expected = {"libs/" + n for n in (*CUDA_LIBS, CUDA_JSON)}
            if (len(members) > MAX_MEMBERS or len(set(names)) != len(names)
                    or {m.name for m in members if m.isfile()} != expected):
                raise RuntimeError("Invalid CUDA archive entries")
            size = 0
            for m in members:
                p = PurePosixPath(m.name)
                if (not p.parts or p.parts[0] != "libs" or p.is_absolute()
                        or ".." in p.parts or not (m.isfile() or m.isdir())):
                    raise RuntimeError(f"Unsafe CUDA archive member: {m.name}")
                size += m.size
                if size > MAX_EXTRACT_BYTES:
                    raise RuntimeError("CUDA archive exceeds extraction limit")
            tar.extractall(path=temp, members=members, filter="data")
        folder = temp / "libs"
        if (folder / CUDA_JSON).read_bytes() != published:
            raise RuntimeError("Embedded CUDA-LIBS.json does not match published file")
        for name in CUDA_LIBS:
            file = folder / name
            if not file.is_file() or file.is_symlink():
                raise RuntimeError(f"Missing CUDA library: {name}")
            digest = hashlib.sha256()
            with file.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            recorded = parsed["libraries"][name]
            if file.stat().st_size != recorded["size"] or digest.hexdigest() != recorded["sha256"]:
                raise RuntimeError(f"Downloaded CUDA library hash mismatch: {name}")
        os.replace(folder, target)
        log("[LIBS] Downloaded CUDA libraries verified and staged")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def download_archive(asset, root, sha256):
    filename = asset["name"]
    path = root / filename
    partial = root / (filename + ".part")
    partial.unlink(missing_ok=True)
    try:
        hashed = hashlib.sha256()
        count = 0
        total = asset.get("size")
        if not isinstance(total, int) or total <= 0:
            raise RuntimeError("Release asset has no valid size")
        next_percent = 5
        log(f"[DOWNLOAD] 0% (0.0 / {total / 1024**2:.1f} MiB)")
        with request(asset["browser_download_url"], timeout=60) as response, partial.open("wb") as stream:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > MAX_ARCHIVE_BYTES:
                    raise RuntimeError("Archive exceeds configured size limit")
                hashed.update(block)
                stream.write(block)
                while next_percent <= 95 and count * 100 >= next_percent * total:
                    log(f"[DOWNLOAD] {next_percent}% ({count / 1024**2:.1f} / {total / 1024**2:.1f} MiB)")
                    next_percent += 5
        if count != total:
            raise RuntimeError("Downloaded archive size does not match release metadata")
        if hashed.hexdigest() != sha256 or hashed.hexdigest() != asset_digest(asset):
            raise RuntimeError("Downloaded archive failed SHA-256 verification")
        log(f"[DOWNLOAD] 100% ({count / 1024**2:.1f} / {total / 1024**2:.1f} MiB)")
        log("[VERIFY] Archive SHA-256 OK")
        os.replace(partial, path)
        return path
    finally:
        partial.unlink(missing_ok=True)


def version_dirs(tag):
    match = TAG_RE.fullmatch(tag)
    if not match:
        raise RuntimeError(f"Unsupported release tag: {tag}")
    compact = match.group(1).replace(".", "")
    return f"strata-v{compact}-sm120", f"server-v{compact}"


def validate_component_paths(engine, server):
    for path in (engine / "strata", engine / "strata-vision", engine / "BUILD.json", engine / "engine-component.json",
                 server / "serve/server.py", server / "serve/web/index.html", server / "serve/web/app.js",
                 server / "requirements.txt", server / "strata.py", server / "SOURCE-SHA.txt"):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Missing or invalid release file: {path}")
    for name in CUDA_LIBS:
        if (engine / name).exists():
            raise RuntimeError(f"CUDA library unexpectedly included in Engine: {name}")
    for path in (engine / "strata", engine / "strata-vision"):
        if not os.access(path, os.X_OK):
            raise RuntimeError(f"Binary is not executable: {path}")
    for dirname in (server / "serve", server / "strata_control", server / "tools"):
        if not dirname.is_dir() or dirname.is_symlink():
            raise RuntimeError(f"Missing release directory: {dirname}")


def extract_archive(archive, root, tag):
    engine_name, server_name = version_dirs(tag)
    staging = Path(tempfile.mkdtemp(prefix=".strata-updater-extract-", dir=root))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_MEMBERS:
                raise RuntimeError("Too many files in release archive")
            size = 0
            roots = set()
            for member in members:
                path = PurePosixPath(member.name)
                if (path.is_absolute() or any(p in {"..", "."} for p in path.parts)
                        or not path.parts or not (member.isfile() or member.isdir())
                        or member.name.startswith("/")):
                    raise RuntimeError(f"Unsafe archive member: {member.name}")
                roots.add(path.parts[0])
                size += member.size
                if size > MAX_EXTRACT_BYTES:
                    raise RuntimeError("Archive expands beyond configured size limit")
            if roots != {engine_name, server_name}:
                raise RuntimeError(f"Unexpected archive directories: {sorted(roots)}")
            # Validation above rejects links, special files and path traversal.
            tar.extractall(path=staging, members=members, filter="data")
        engine, server = staging / engine_name, staging / server_name
        validate_component_paths(engine, server)
        # Keep a complete, unpacked copy of each release for rollback and retention.
        for name in (engine_name, server_name):
            dest = root / name
            if dest.is_symlink():
                raise RuntimeError(f"Refusing to replace symbolic link: {dest}")
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(staging / name, dest)
        return root / engine_name, root / server_name
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def http_json(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {url}")
        return json.loads(response.read(1024 * 1024))


def status_url(base):
    return base.rstrip("/") + "/v1/status"


def can_stop(base):
    data = http_json(status_url(base))
    activity = data.get("activity")
    return data.get("service") == "strata" and data.get("loaded") is False and \
        isinstance(activity, dict) and activity.get("in_flight") == 0


class SupersededRelease(RuntimeError):
    """A newer compatible release superseded a staged, waiting update."""


def check_for_superseded_release(expected_tag, expected_sha):
    _, latest, archive, *_ = releases()
    latest_tag = latest["tag_name"]
    if (tuple(map(int, TAG_RE.fullmatch(latest_tag).group(1).split("."))) >
            tuple(map(int, TAG_RE.fullmatch(expected_tag).group(1).split(".")))):
        raise SupersededRelease(f"{expected_tag} superseded by {latest_tag}")
    if latest_tag == expected_tag and asset_digest(archive) != expected_sha:
        raise SupersededRelease(f"{expected_tag} archive has been replaced")


def wait_for_idle(systemctl, base, candidate_tag, candidate_sha):
    last_report = -float("inf")
    last_self_check = time.monotonic()
    last_release_check = time.monotonic()
    while True:
        # Check both the updater source and newer Strata releases while the model runs.
        # Neither check stops Strata or modifies active files.
        now = time.monotonic()
        requested = SELF_UPDATE_REQUESTED.is_set()
        if requested or now - last_self_check >= INTERVAL:
            SELF_UPDATE_REQUESTED.clear()
            last_self_check = now
            try:
                if not perform_self_update(base) and requested:
                    log("[SELF-UPDATE] Updater already current")
            except Exception as exc:
                log(f"[SELF-UPDATE] Check failed: {exc}")
        if WAKE.is_set() or now - last_release_check >= INTERVAL:
            WAKE.clear()
            last_release_check = now
            try:
                check_for_superseded_release(candidate_tag, candidate_sha)
                log(f"[CHECK] {candidate_tag} is still the newest compatible release")
            except SupersededRelease:
                raise
            except Exception as exc:
                log(f"[CHECK] Unable to refresh release while waiting: {exc}")
        active = service_state(systemctl)
        if active == "inactive":
            log("[WAIT] Strata is stopped; a temporary verification start will follow installation")
            return False
        if active == "active":
            try:
                if can_stop(base):
                    log("[WAIT] Model unloaded and no requests in flight")
                    return True
                message = "Model loaded or requests pending; waiting for automatic unload"
            except (ValueError, OSError, KeyError, RuntimeError) as exc:
                message = f"Strata status unavailable; waiting without stopping service: {exc}"
        else:
            message = f"Waiting for stable Strata service state: {active}"
        now = time.monotonic()
        if now - last_report >= 5 * 60:
            log(f"[WAIT] {message}; checking every {POLL}s")
            last_report = now
        WAKE.wait(POLL)


def confirm_started(systemctl, base):
    deadline = time.monotonic() + HEALTH_TIMEOUT
    good = 0
    while time.monotonic() < deadline:
        if service_state(systemctl) != "active":
            good = 0
        else:
            try:
                health = http_json(base.rstrip("/") + "/health")
                status = http_json(status_url(base))
                if health.get("status") == "ok" and health.get("service") == "strata" and \
                        status.get("service") == "strata" and isinstance(status.get("loaded"), bool):
                    good += 1
                    if good >= 2:
                        log("[HEALTH] Strata service and HTTP API OK")
                        return
                else:
                    good = 0
            except (OSError, ValueError, RuntimeError):
                good = 0
        time.sleep(2)
    raise RuntimeError("Strata did not pass the startup health checks")


def temp_backup(root):
    return root / ".strata-updater-backup"


def selected_previous(root, state, new_tag):
    old_tag = state.get("current_tag")
    if old_tag == new_tag:
        old_tag = state.get("previous_tag")
    if old_tag and old_tag != new_tag:
        pair = version_dirs(old_tag)
        if all((root / name).is_dir() for name in pair):
            return old_tag, pair
    pair = state.get("previous_dirs")
    if isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair):
        if pair != list(version_dirs(new_tag)) and all((root / name).is_dir() for name in pair):
            return state.get("previous_tag"), tuple(pair)
    # First installation: preserve the most recently populated complete old pair.
    choices = []
    current_names = set(version_dirs(new_tag))
    for item in root.iterdir():
        match = ENGINE_RE.fullmatch(item.name)
        if not match or item.name in current_names or item.is_symlink():
            continue
        companion = root / f"server-v{match.group(1)}"
        if companion.is_dir() and not companion.is_symlink():
            choices.append((max(item.stat().st_mtime, companion.stat().st_mtime), (item.name, companion.name)))
    return (None, max(choices)[1]) if choices else (None, None)


def retain_two(root, current_tag, previous_pair):
    keep_names = set(version_dirs(current_tag))
    if previous_pair:
        keep_names.update(previous_pair)
    for child in root.iterdir():
        if child.name in keep_names or child.is_symlink() or not child.is_dir():
            continue
        if ENGINE_RE.fullmatch(child.name) or SERVER_RE.fullmatch(child.name):
            log(f"Removing obsolete release directory: {child.name}")
            shutil.rmtree(child)


def finalize_committed(root, state, systemctl):
    tx = state.get("transaction")
    if not tx or tx.get("phase") != "committed":
        return
    backup = temp_backup(root)
    log("[CLEANUP] Removing temporary backups and obsolete release directories")
    if backup.exists():
        shutil.rmtree(backup)
    retain_two(root, state["current_tag"], state.get("previous_dirs"))
    if not tx.get("original_active"):
        # Restore the operator's original stopped state only AFTER cleanup.
        if service_state(systemctl) != "inactive":
            control(systemctl, "stop")
        if service_state(systemctl) != "inactive":
            raise RuntimeError("Verification service did not return to the stopped state")
    state.pop("transaction", None)
    save_state(state)
    log("[CLEANUP] Complete; current and previous release retained")


def restore_transaction(root, state, systemctl):
    tx = state.get("transaction")
    if not tx:
        return
    if tx.get("phase") == "committed":
        finalize_committed(root, state, systemctl)
        return
    log("[ROLLBACK] Recovering interrupted installation")
    backup = temp_backup(root)
    original_active = bool(tx.get("original_active"))
    if backup.exists() and any((backup / name).exists() for name in ("engine", "server", "libs")):
        control(systemctl, "stop")
        for name, dest_name in (("engine", "strata-sm120"), ("server", "server"), ("libs", "libs")):
            saved, dest = backup / name, root / dest_name
            if saved.exists():
                if dest.is_symlink():
                    raise RuntimeError(f"Refusing symlink during rollback: {dest}")
                if dest.exists():
                    shutil.rmtree(dest)
                os.replace(saved, dest)
        if tx.get("libs_changed") and not tx.get("libs_present"):
            libdir = root / "libs"
            if libdir.is_symlink():
                raise RuntimeError("Refusing symlink during CUDA rollback")
            if libdir.exists():
                shutil.rmtree(libdir)
    if original_active:
        control(systemctl, "start")
    elif service_state(systemctl) != "inactive":
        control(systemctl, "stop")
    if backup.exists():
        shutil.rmtree(backup)
    for name in (".strata-updater-new-engine", ".strata-updater-new-server",
                 ".strata-updater-new-libs"):
        shutil.rmtree(root / name, ignore_errors=True)
    state.pop("transaction", None)
    save_state(state)
    log("[ROLLBACK] Previous working directories restored")


def install_release(root, state, tag, digest, systemctl, api_url, cuda_json_sha, replace_libs):
    engine_name, server_name = version_dirs(tag)
    originals = (root / "strata-sm120", root / "server")
    sources = (root / engine_name, root / server_name)
    validate_component_paths(*sources)
    if not all(x.is_dir() and not x.is_symlink() for x in originals):
        raise RuntimeError("Both working directories must exist as real directories")
    libs = root / "libs"
    staged_libs = root / ".strata-updater-new-libs"
    if libs.is_symlink():
        raise RuntimeError("CUDA library directory cannot be a symlink")
    if replace_libs:
        if not staged_libs.is_dir() or staged_libs.is_symlink():
            raise RuntimeError("Verified CUDA staging directory is missing")
    elif not libs.is_dir() or any(not (libs / n).is_file() for n in CUDA_LIBS):
        raise RuntimeError("Installed CUDA libraries are missing")
    original_active = wait_for_idle(systemctl, api_url, tag, digest)
    backup = temp_backup(root)
    if backup.exists():
        raise RuntimeError("Unresolved backup directory; refusing update")
    previous_tag, previous_dirs = selected_previous(root, state, tag)
    state["transaction"] = {"phase": "prepared", "original_active": original_active, "tag": tag,
                            "libs_changed": replace_libs, "libs_present": libs.is_dir()}
    save_state(state)
    try:
        if original_active:
            log("[UPDATE] Stopping Strata")
            control(systemctl, "stop")
        else:
            log("[UPDATE] Strata was stopped; retaining its original stopped state")
        if service_state(systemctl) != "inactive":
            raise RuntimeError("Strata did not stop cleanly")
        log(f"[INSTALL] Installing {tag}: Engine and Server")
        backup.mkdir(mode=0o700)
        staged_engine = root / ".strata-updater-new-engine"
        staged_server = root / ".strata-updater-new-server"
        for dest in (staged_engine, staged_server):
            if dest.exists():
                shutil.rmtree(dest)
        shutil.copytree(sources[0], staged_engine, symlinks=False)
        shutil.copytree(sources[1], staged_server, symlinks=False)
        validate_component_paths(staged_engine, staged_server)
        os.replace(originals[0], backup / "engine")
        os.replace(originals[1], backup / "server")
        os.replace(staged_engine, originals[0])
        os.replace(staged_server, originals[1])
        if replace_libs:
            if libs.exists():
                os.replace(libs, backup / "libs")
            os.replace(staged_libs, libs)
            log("[LIBS] Installed verified CUDA libraries; saved previous library set")
        else:
            log("[LIBS] Retaining existing CUDA libraries")
        log("[START] Starting Strata for verification")
        control(systemctl, "start")
        confirm_started(systemctl, api_url)
        state.update(current_tag=tag, current_sha256=digest, cuda_json_sha256=cuda_json_sha,
                     previous_tag=previous_tag,
                     previous_dirs=list(previous_dirs) if previous_dirs else None,
                     installed_at=dt.datetime.now(dt.timezone.utc).isoformat())
        state.pop("pending", None)
        state.pop("failed_digest", None)
        state["transaction"]["phase"] = "committed"
        save_state(state)
        # The new installation is now committed. Cleanup errors must not roll back
        # an otherwise healthy installation; resume cleanup on the next iteration.
        try:
            finalize_committed(root, state, systemctl)
        except Exception as exc:
            log(f"[CLEANUP] Installed new version, cleanup pending: {exc}")
            if not original_active:
                try:
                    control(systemctl, "stop")
                except Exception as stop_error:
                    log(f"[CLEANUP] Unable to restore the stopped state yet: {stop_error}")
        if state.get("transaction"):
            log(f"[UPDATE] Installed {tag}; cleanup pending")
        else:
            log(f"[DONE] Updated to {tag}; Strata {'running' if original_active else 'stopped'}")
    except Exception:
        try:
            restore_transaction(root, state, systemctl)
        finally:
            for name in (".strata-updater-new-engine", ".strata-updater-new-server",
                         ".strata-updater-new-libs"):
                shutil.rmtree(root / name, ignore_errors=True)
        raise



def install_candidate(root, state, release, archive, cuda_archive, cuda_meta, checksums, systemctl, api_url):
    tag = release["tag_name"]
    core_sha = asset_digest(archive)
    if state.get("current_tag") == tag and state.get("current_sha256") == core_sha:
        log(f"[SKIP] Already installed: {tag}")
        return
    old, new = TAG_RE.fullmatch(state.get("current_tag") or ""), TAG_RE.fullmatch(tag)
    if old and new:
        if tuple(map(int, new.group(1).split("."))) < tuple(map(int, old.group(1).split("."))):
            log(f"[SKIP] Refusing automatic downgrade to {tag}")
            return
    if state.get("failed_digest") == core_sha:
        log(f"[SKIP] Previously failed package: {tag}")
        return

    checks = read_checksums(checksums)
    if expected_checksum(checks, archive["name"]) != core_sha:
        raise RuntimeError("Engine archive SHA-256 disagrees with checksum list")
    cuda_sha = asset_digest(cuda_archive)
    if expected_checksum(checks, cuda_archive["name"]) != cuda_sha:
        raise RuntimeError("CUDA archive SHA-256 disagrees with checksum list")
    published, parsed = read_cuda_asset(cuda_meta, checks)
    update_libs = not same_cuda_libraries(root, published)
    if update_libs:
        log("[LIBS] CUDA-LIBS.json differs: separate CUDA archive required")
    else:
        log("[LIBS] CUDA-LIBS.json matches: no library download or rehash")

    engine_name, server_name = version_dirs(tag)
    pending = state.get("pending")
    reuse = (isinstance(pending, dict) and pending.get("tag") == tag
             and pending.get("sha256") == core_sha
             and (root / engine_name).is_dir() and (root / server_name).is_dir())
    if not reuse:
        log(f"[DOWNLOAD] Downloading {archive['name']}")
        downloaded = download_archive(archive, root, core_sha)
        try:
            extract_archive(downloaded, root, tag)
        finally:
            downloaded.unlink(missing_ok=True)
        state["pending"] = {"tag": tag, "sha256": core_sha}
        save_state(state)
    else:
        validate_component_paths(root / engine_name, root / server_name)
        log("[EXTRACT] Reusing verified Engine and Server")
    if update_libs:
        log(f"[DOWNLOAD] Downloading {cuda_archive['name']}")
        downloaded = download_archive(cuda_archive, root, cuda_sha)
        try:
            stage_cuda_archive(downloaded, root, published, parsed)
        finally:
            downloaded.unlink(missing_ok=True)
    else:
        stage = root / ".strata-updater-new-libs"
        if stage.is_symlink():
            raise RuntimeError("Refusing CUDA staging symlink")
        if stage.exists():
            shutil.rmtree(stage)
    log(f"[WAIT] Prepared {tag}; monitoring Strata until safe to install")
    try:
        install_release(root, state, tag, core_sha, systemctl, api_url,
                        hashlib.sha256(published).hexdigest(), update_libs)
    except SupersededRelease as exc:
        log(f"[CHECK] {exc}; preparing latest release without stopping Strata")
        return True
    except Exception as exc:
        state["failed_digest"] = core_sha
        state.pop("pending", None)
        save_state(state)
        raise RuntimeError(f"Installation failed; rollback attempted: {exc}") from exc

def run_daemon(root, api_url):
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise RuntimeError("systemctl is required")
    root.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    lock_path = state_file().parent / "updater.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("An updater process is already running") from exc
    state = read_state()
    restore_transaction(root, state, systemctl)

    def wake(signum, frame):
        WAKE.set()
    def update_requested(signum, frame):
        SELF_UPDATE_REQUESTED.set()
        WAKE.set()
    signal.signal(signal.SIGUSR1, wake)
    signal.signal(signal.SIGUSR2, update_requested)
    log(f"[START] Updater {VERSION} started; release checks every 6 hours")
    while True:
        try:
            restore_transaction(root, state, systemctl)
            requested = SELF_UPDATE_REQUESTED.is_set()
            SELF_UPDATE_REQUESTED.clear()
            try:
                if not perform_self_update(api_url) and requested:
                    log("[SELF-UPDATE] Updater already current")
            except Exception as exc:
                log(f"[SELF-UPDATE] Check failed: {exc}")
            log("[CHECK] Checking GitHub releases")
            _, release, archive, cuda_archive, cuda_meta, checksums = releases()
            state["last_checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_state(state)
            superseded = install_candidate(root, state, release, archive, cuda_archive, cuda_meta,
                                           checksums, systemctl, api_url)
            if superseded:
                continue
        except Exception as exc:
            log(f"[ERROR] Update check failed: {exc}")
        WAKE.wait(INTERVAL)
        WAKE.clear()


def systemd_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def sudo_install(source, dest, mode):
    run_command(["sudo", "install", "-m", mode, str(source), str(dest)])


def install_self(api_url):
    if os.geteuid() == 0:
        raise RuntimeError("Run --install as the intended non-root service user, without sudo")
    account = pwd.getpwuid(os.geteuid())
    username = account.pw_name
    if not re.fullmatch(r"[a-z_][a-z0-9_.-]*\$?", username):
        raise RuntimeError("Unsupported Linux username for restricted sudoers policy")
    systemctl = shutil.which("systemctl")
    if not systemctl or not shutil.which("sudo"):
        raise RuntimeError("systemd and sudo are required")
    if not shutil.which("visudo") and not Path("/usr/sbin/visudo").exists():
        raise RuntimeError("visudo is required")
    base = home() / "strata"
    if not all((base / d).is_dir() for d in ("strata-sm120", "server")):
        raise RuntimeError("Expected ~/strata/strata-sm120/ and ~/strata/server/ to exist")
    try:
        owner = run_command([systemctl, "show", "-p", "User", "--value", SERVICE]).stdout.strip()
    except RuntimeError as exc:
        raise RuntimeError("strata.service must be installed before the updater") from exc
    if owner != username:
        raise RuntimeError(f"strata.service User={owner!r} differs from the installing user")
    source = Path(__file__).resolve()
    if not source.is_file():
        raise RuntimeError("Install from a downloaded .py file, not from standard input")
    path = home() / ".local/bin/strata-updater.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    if source != path:
        shutil.copy2(source, path)
    path.chmod(0o755)
    unit = f"""[Unit]
Description=Automatic Strata CUDA release updater
Wants=network-online.target
After=network-online.target
RequiresMountsFor={base}

[Service]
Type=simple
User={username}
Group={grp.getgrgid(account.pw_gid).gr_name}
WorkingDirectory={base}
ExecStart={systemd_quote(sys.executable)} {systemd_quote(str(path))} --run --api-url {systemd_quote(api_url)}
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
"""
    rule = f"{username} ALL=(root) NOPASSWD: {systemctl} start {SERVICE}, {systemctl} stop {SERVICE}\n"
    sudo_file = Path(f"/etc/sudoers.d/strata-updater-{account.pw_uid}")
    unit_file = Path(f"/etc/systemd/system/{UPDATER}")
    with tempfile.TemporaryDirectory(prefix="strata-updater-install-") as d:
        work = Path(d)
        unit_tmp, sudo_tmp = work / UPDATER, work / "sudoers"
        unit_tmp.write_text(unit, encoding="utf-8")
        sudo_tmp.write_text(rule, encoding="utf-8")
        run_command(["sudo", "-v"], timeout=120)
        # Validate a temporary candidate before installing a privileged policy.
        run_command(["sudo", "/usr/sbin/visudo", "-cf", str(sudo_tmp)])
        analyzer = shutil.which("systemd-analyze")
        if analyzer:
            run_command([analyzer, "verify", str(unit_tmp)])
        sudo_install(sudo_tmp, sudo_file, "0440")
        sudo_install(unit_tmp, unit_file, "0644")
        run_command(["sudo", systemctl, "daemon-reload"])
        run_command(["sudo", systemctl, "enable", "--now", UPDATER])
        if run_command([systemctl, "is-active", "--quiet", UPDATER], check=False).returncode:
            raise RuntimeError(f"{UPDATER} was installed but did not start; inspect journalctl -u {UPDATER} -n 50")
    log(f"Installed and started {UPDATER} for user {username}; release root: {base}")


def uninstall_self():
    if os.geteuid() == 0:
        raise RuntimeError("Run --uninstall as the same non-root user used for installation")
    account = pwd.getpwuid(os.geteuid())
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise RuntimeError("systemctl required")
    unit_file = Path(f"/etc/systemd/system/{UPDATER}")
    if unit_file.exists():
        # Refuse to remove an updater installed for another account.
        assigned = run_command([systemctl, "show", "-p", "User", "--value", UPDATER]).stdout.strip()
        if assigned != account.pw_name:
            raise RuntimeError("The installed updater belongs to another user")
    run_command(["sudo", "-v"], timeout=120)
    run_command(["sudo", systemctl, "disable", "--now", UPDATER], check=False)
    run_command(["sudo", "rm", "-f", str(unit_file)])
    run_command(["sudo", "rm", "-f", f"/etc/sudoers.d/strata-updater-{account.pw_uid}"])
    run_command(["sudo", systemctl, "daemon-reload"])
    (home() / ".local/bin/strata-updater.py").unlink(missing_ok=True)
    log("Updater removed; Strata, release folders and updater state were left untouched")



def manual_self_update():
    """Request daemon update and wait briefly; never interrupt a Strata installation."""
    if os.geteuid() == 0:
        raise RuntimeError("Run --update as the service owner, without sudo")
    path = home() / ".local/bin/strata-updater.py"
    if not path.is_file():
        raise RuntimeError("Updater is not installed")
    systemctl = shutil.which("systemctl") or "systemctl"
    owner = run_command([systemctl, "show", "-p", "User", "--value", UPDATER]).stdout.strip()
    if owner != pwd.getpwuid(os.geteuid()).pw_name:
        raise RuntimeError("Updater service belongs to another account")
    pid = run_command([systemctl, "show", "-p", "MainPID", "--value", UPDATER]).stdout.strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise RuntimeError("Start the updater service before using --update")
    payload, version = fetch_updater_source()
    if path.read_bytes() == payload:
        log(f"[SELF-UPDATE] Already current: {version}")
        return
    os.kill(int(pid), signal.SIGUSR2)
    log(f"[SELF-UPDATE] Requested {version}; waiting for safe restart")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if path.read_bytes() == payload:
            time.sleep(2)
            active = run_command([systemctl, "is-active", UPDATER], check=False).stdout.strip()
            if active == "active":
                log(f"[SELF-UPDATE] Installed {version}; service active")
                return
        time.sleep(2)
    log("[SELF-UPDATE] Update queued until current operation finishes; see journalctl")


def main():
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required")
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    for name in ("install", "uninstall", "run", "status", "check-now", "update", "version"):
        choice.add_argument("--" + name, action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8090", help="Local Strata HTTP API")
    options = parser.parse_args()
    if options.version:
        print(VERSION)
    elif options.install:
        install_self(options.api_url)
    elif options.uninstall:
        uninstall_self()
    elif options.run:
        run_daemon(home() / "strata", options.api_url)
    elif options.status:
        state = read_state()
        state["updater_service"] = run_command([shutil.which("systemctl") or "systemctl", "is-active", UPDATER], check=False).stdout.strip()
        print(json.dumps(state, indent=2, sort_keys=True))
    elif options.update:
        manual_self_update()
    elif options.check_now:
        systemctl = shutil.which("systemctl") or "systemctl"
        pid = run_command([systemctl, "show", "-p", "MainPID", "--value", UPDATER]).stdout.strip()
        if not pid.isdigit() or int(pid) <= 0:
            raise RuntimeError("Updater service is not running")
        os.kill(int(pid), signal.SIGUSR1)
        log("Immediate release check requested")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, tarfile.TarError, urllib.error.URLError) as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
