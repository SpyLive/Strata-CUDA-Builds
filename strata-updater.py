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

VERSION = "1.0.2"
REPO = "SpyLive/Strata-CUDA-Builds"
PREFIX = "strata-sm120-cuda13.2-"
SERVICE = "strata.service"
UPDATER = "strata-updater.service"
INTERVAL = 12 * 60 * 60
POLL = 60
HEALTH_TIMEOUT = 90
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_EXTRACT_BYTES = 4 * 1024**3
MAX_MEMBERS = 25000
MAX_SELF_SOURCE_BYTES = 512 * 1024
TAG_RE = re.compile(r"^strata-sm120-cuda13\.2-v(\d+\.\d+\.\d+(?:\.\d+)?)$")
ENGINE_RE = re.compile(r"^strata-v(\d+)-sm120$")
SERVER_RE = re.compile(r"^server-v(\d+)$")
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
    """Fetch updater from main and cross-check source with GitHub's blob checksum."""
    url = f"https://api.github.com/repos/{REPO}/contents/strata-updater.py?ref=main"
    with request(url, timeout=35) as response:
        info = json.loads(response.read(MAX_SELF_SOURCE_BYTES * 2))
    encoded = info.get("content")
    if info.get("encoding") != "base64" or not isinstance(encoded, str):
        raise RuntimeError("Invalid updater source response from GitHub")
    payload = base64.b64decode("".join(encoded.split()), validate=True)
    if not payload or len(payload) > MAX_SELF_SOURCE_BYTES:
        raise RuntimeError("Updater source is empty or exceeds size limit")
    blob_sha = hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()
    if blob_sha != info.get("sha"):
        raise RuntimeError("Updater source does not match GitHub blob checksum")
    source = payload.decode("utf-8")
    if not source.startswith("#!/usr/bin/env python3"):
        raise RuntimeError("Invalid updater source header")
    compile(source, "strata-updater.py", "exec")
    match = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"
    url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
    with request(url) as response:
        data = json.load(response)
    options = []
    for release in data:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = release.get("tag_name", "")
        match = TAG_RE.fullmatch(tag)
        if not match:
            continue
        version = tuple(map(int, match.group(1).split(".")))
        upstream = tag[len(PREFIX):]
        expected = f"strata-engine-{upstream}-cuda13.2-sm120-linux-x86_64.tar.gz"
        by_name = {a.get("name"): a for a in release.get("assets", [])}
        archive = by_name.get(expected)
        checksums = by_name.get("SHA256SUMS.txt")
        if archive and checksums and archive.get("browser_download_url") and checksums.get("browser_download_url"):
            options.append((version, release, archive, checksums))
    if not options:
        raise RuntimeError("No compatible releases with archive and checksums")
    return max(options, key=lambda item: item[0])


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
                 engine / "libcudart.so.13", engine / "libcublas.so.13", engine / "libcublasLt.so.13",
                 server / "serve/server.py", server / "serve/web/index.html", server / "serve/web/app.js",
                 server / "requirements.txt", server / "strata.py", server / "SOURCE-SHA.txt"):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Missing or invalid release file: {path}")
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


def wait_for_idle(systemctl, base):
    last_report = -float("inf")
    while True:
        # No Strata files have been switched at this point.
        if SELF_UPDATE_REQUESTED.is_set():
            SELF_UPDATE_REQUESTED.clear()
            try:
                if not perform_self_update(base):
                    log("[SELF-UPDATE] Updater already matches GitHub main")
            except Exception as exc:
                log(f"[SELF-UPDATE] Could not refresh updater: {exc}")
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
        time.sleep(POLL)


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
    if backup.exists() and any((backup / name).exists() for name in ("engine", "server")):
        control(systemctl, "stop")
        for name, dest_name in (("engine", "strata-sm120"), ("server", "server")):
            saved = backup / name
            dest = root / dest_name
            if saved.exists():
                if dest.exists():
                    shutil.rmtree(dest)
                os.replace(saved, dest)
    if original_active:
        control(systemctl, "start")
    elif service_state(systemctl) != "inactive":
        control(systemctl, "stop")
    if backup.exists():
        shutil.rmtree(backup)
    for name in (".strata-updater-new-engine", ".strata-updater-new-server"):
        shutil.rmtree(root / name, ignore_errors=True)
    state.pop("transaction", None)
    save_state(state)
    log("[ROLLBACK] Previous working directories restored")


def install_release(root, state, tag, digest, systemctl, api_url):
    engine_name, server_name = version_dirs(tag)
    originals = (root / "strata-sm120", root / "server")
    sources = (root / engine_name, root / server_name)
    validate_component_paths(*sources)
    if not all(x.is_dir() and not x.is_symlink() for x in originals):
        raise RuntimeError("Both working directories must exist as real directories")
    original_active = wait_for_idle(systemctl, api_url)
    backup = temp_backup(root)
    if backup.exists():
        raise RuntimeError("Unresolved backup directory; refusing update")
    previous_tag, previous_dirs = selected_previous(root, state, tag)
    state["transaction"] = {"phase": "prepared", "original_active": original_active, "tag": tag}
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
        log("[START] Starting Strata for verification")
        control(systemctl, "start")
        confirm_started(systemctl, api_url)
        state.update(current_tag=tag, current_sha256=digest, previous_tag=previous_tag,
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
            for name in (".strata-updater-new-engine", ".strata-updater-new-server"):
                shutil.rmtree(root / name, ignore_errors=True)
        raise


def install_candidate(root, state, release, archive, checksums, systemctl, api_url):
    tag = release["tag_name"]
    archive_sha = asset_digest(archive)
    if state.get("current_tag") == tag and state.get("current_sha256") == archive_sha:
        log(f"[SKIP] Already installed: {tag}")
        return
    existing = TAG_RE.fullmatch(state.get("current_tag") or "")
    incoming = TAG_RE.fullmatch(tag)
    if existing and incoming:
        old_v = tuple(map(int, existing.group(1).split(".")))
        new_v = tuple(map(int, incoming.group(1).split(".")))
        if new_v < old_v:
            log(f"[SKIP] Refusing automatic downgrade from {state['current_tag']} to {tag}")
            return
    if state.get("failed_digest") == archive_sha:
        log(f"[SKIP] Previously failed package: {tag}; awaiting a different release")
        return
    log(f"[FOUND] New compatible package: {tag}")
    engine_name, server_name = version_dirs(tag)
    pending = state.get("pending")
    if not (isinstance(pending, dict) and pending.get("tag") == tag and
            pending.get("sha256") == archive_sha and
            (root / engine_name).is_dir() and (root / server_name).is_dir()):
        text = read_checksums(checksums)
        checked_sha = expected_checksum(text, archive["name"])
        if checked_sha != archive_sha:
            raise RuntimeError("SHA256SUMS.txt and GitHub asset digest disagree")
        log(f"[DOWNLOAD] Downloading {archive['name']}")
        file = download_archive(archive, root, checked_sha)
        try:
            log("[EXTRACT] Extracting Engine and Server")
            extract_archive(file, root, tag)
            log("[EXTRACT] Engine and Server ready")
        finally:
            file.unlink(missing_ok=True)
        state["pending"] = {"tag": tag, "sha256": archive_sha}
        save_state(state)
    else:
        validate_component_paths(root / engine_name, root / server_name)
        log("[EXTRACT] Reusing previously prepared Engine and Server")
    log(f"[WAIT] Prepared {tag}; monitoring Strata until safe to install")
    try:
        install_release(root, state, tag, archive_sha, systemctl, api_url)
    except Exception as exc:
        # Never permanently retry a broken build each minute or each scheduled check.
        state["failed_digest"] = archive_sha
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
    log(f"[START] Updater {VERSION} started; release checks every 12 hours")
    while True:
        try:
            restore_transaction(root, state, systemctl)
            requested = SELF_UPDATE_REQUESTED.is_set()
            SELF_UPDATE_REQUESTED.clear()
            try:
                if not perform_self_update(api_url) and requested:
                    log("[SELF-UPDATE] Updater already matches GitHub main")
            except Exception as exc:
                log(f"[SELF-UPDATE] Could not refresh updater: {exc}")
            log("[CHECK] Checking GitHub releases")
            _, release, archive, checksums = releases()
            state["last_checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_state(state)
            install_candidate(root, state, release, archive, checksums, systemctl, api_url)
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
WorkingDirectory={systemd_quote(str(base))}
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
        unit_tmp, sudo_tmp = work / "unit", work / "sudoers"
        unit_tmp.write_text(unit, encoding="utf-8")
        sudo_tmp.write_text(rule, encoding="utf-8")
        run_command(["sudo", "-v"], timeout=120)
        # Validate a temporary candidate before installing a privileged policy.
        run_command(["sudo", "/usr/sbin/visudo", "-cf", str(sudo_tmp)])
        sudo_install(sudo_tmp, sudo_file, "0440")
        sudo_install(unit_tmp, unit_file, "0644")
        run_command(["sudo", systemctl, "daemon-reload"])
        run_command(["sudo", systemctl, "enable", "--now", UPDATER])
    log(f"Installed {UPDATER} for user {username}; release root: {base}")


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
    """Ask the running updater to upgrade; it applies the update at a safe point."""
    if os.geteuid() == 0:
        raise RuntimeError("Run --update as the updater service user, without sudo")
    installed = home() / ".local/bin/strata-updater.py"
    if not installed.is_file():
        raise RuntimeError("Updater is not installed")
    systemctl = shutil.which("systemctl") or "systemctl"
    owner = run_command([systemctl, "show", "-p", "User", "--value", UPDATER]).stdout.strip()
    if owner != pwd.getpwuid(os.geteuid()).pw_name:
        raise RuntimeError("Updater service belongs to another account")
    pid = run_command([systemctl, "show", "-p", "MainPID", "--value", UPDATER]).stdout.strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise RuntimeError("Updater service must be running for --update")
    source, version = fetch_updater_source()
    if installed.read_bytes() == source:
        log(f"[SELF-UPDATE] Already current: {version}")
        return
    os.kill(int(pid), signal.SIGUSR2)
    log(f"[SELF-UPDATE] Requested {version}; waiting for safe restart")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if installed.read_bytes() == source:
            time.sleep(2)
            active = run_command([systemctl, "is-active", UPDATER], check=False).stdout.strip()
            if active == "active":
                log(f"[SELF-UPDATE] Updated to {version}; service is active")
                return
        time.sleep(2)
    log("[SELF-UPDATE] Update queued until current operation finishes; see systemd journal")


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
, source, re.MULTILINE)
    if not match:
        raise RuntimeError("Downloaded updater does not declare a version")
    version = match.group(1)
    if tuple(map(int, version.split("."))) < tuple(map(int, VERSION.split("."))):
        raise RuntimeError(f"Refusing updater downgrade from {VERSION} to {version}")
    return payload, version


def write_updater_atomically(path, source, version):
    fd, name = tempfile.mkstemp(prefix=".strata-updater-upgrade-", dir=path.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        staging.chmod(0o755)
        smoke = run_command([sys.executable, str(staging), "--version"], timeout=20)
        if smoke.stdout.strip() != version:
            raise RuntimeError("Updated script failed its startup smoke test")
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def perform_self_update(api_url):
    """Apply updates only at safe points. execv preserves systemd's tracked PID."""
    installed = home() / ".local/bin/strata-updater.py"
    if not installed.is_file() or installed.resolve() != Path(__file__).resolve():
        raise RuntimeError("Self-update requires installed updater instance")
    source, version = fetch_updater_source()
    previous = installed.read_bytes()
    if source == previous:
        return False
    log(f"[SELF-UPDATE] GitHub updater changed: {VERSION} -> {version}")
    write_updater_atomically(installed, source, version)
    log(f"[SELF-UPDATE] Script {version} installed; restarting updater process")
    try:
        os.execv(sys.executable, [sys.executable, str(installed), "--run", "--api-url", api_url])
    except OSError as exc:
        write_updater_atomically(installed, previous, VERSION)
        raise RuntimeError(f"Could not restart updater; previous script restored: {exc}") from exc


def releases():
    url = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
    with request(url) as response:
        data = json.load(response)
    options = []
    for release in data:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = release.get("tag_name", "")
        match = TAG_RE.fullmatch(tag)
        if not match:
            continue
        version = tuple(map(int, match.group(1).split(".")))
        upstream = tag[len(PREFIX):]
        expected = f"strata-engine-{upstream}-cuda13.2-sm120-linux-x86_64.tar.gz"
        by_name = {a.get("name"): a for a in release.get("assets", [])}
        archive = by_name.get(expected)
        checksums = by_name.get("SHA256SUMS.txt")
        if archive and checksums and archive.get("browser_download_url") and checksums.get("browser_download_url"):
            options.append((version, release, archive, checksums))
    if not options:
        raise RuntimeError("No compatible releases with archive and checksums")
    return max(options, key=lambda item: item[0])


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
                 engine / "libcudart.so.13", engine / "libcublas.so.13", engine / "libcublasLt.so.13",
                 server / "serve/server.py", server / "serve/web/index.html", server / "serve/web/app.js",
                 server / "requirements.txt", server / "strata.py", server / "SOURCE-SHA.txt"):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Missing or invalid release file: {path}")
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


def wait_for_idle(systemctl, base):
    last_report = -float("inf")
    while True:
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
        time.sleep(POLL)


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
    if backup.exists() and any((backup / name).exists() for name in ("engine", "server")):
        control(systemctl, "stop")
        for name, dest_name in (("engine", "strata-sm120"), ("server", "server")):
            saved = backup / name
            dest = root / dest_name
            if saved.exists():
                if dest.exists():
                    shutil.rmtree(dest)
                os.replace(saved, dest)
    if original_active:
        control(systemctl, "start")
    elif service_state(systemctl) != "inactive":
        control(systemctl, "stop")
    if backup.exists():
        shutil.rmtree(backup)
    for name in (".strata-updater-new-engine", ".strata-updater-new-server"):
        shutil.rmtree(root / name, ignore_errors=True)
    state.pop("transaction", None)
    save_state(state)
    log("[ROLLBACK] Previous working directories restored")


def install_release(root, state, tag, digest, systemctl, api_url):
    engine_name, server_name = version_dirs(tag)
    originals = (root / "strata-sm120", root / "server")
    sources = (root / engine_name, root / server_name)
    validate_component_paths(*sources)
    if not all(x.is_dir() and not x.is_symlink() for x in originals):
        raise RuntimeError("Both working directories must exist as real directories")
    original_active = wait_for_idle(systemctl, api_url)
    backup = temp_backup(root)
    if backup.exists():
        raise RuntimeError("Unresolved backup directory; refusing update")
    previous_tag, previous_dirs = selected_previous(root, state, tag)
    state["transaction"] = {"phase": "prepared", "original_active": original_active, "tag": tag}
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
        log("[START] Starting Strata for verification")
        control(systemctl, "start")
        confirm_started(systemctl, api_url)
        state.update(current_tag=tag, current_sha256=digest, previous_tag=previous_tag,
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
            for name in (".strata-updater-new-engine", ".strata-updater-new-server"):
                shutil.rmtree(root / name, ignore_errors=True)
        raise


def install_candidate(root, state, release, archive, checksums, systemctl, api_url):
    tag = release["tag_name"]
    archive_sha = asset_digest(archive)
    if state.get("current_tag") == tag and state.get("current_sha256") == archive_sha:
        log(f"[SKIP] Already installed: {tag}")
        return
    existing = TAG_RE.fullmatch(state.get("current_tag") or "")
    incoming = TAG_RE.fullmatch(tag)
    if existing and incoming:
        old_v = tuple(map(int, existing.group(1).split(".")))
        new_v = tuple(map(int, incoming.group(1).split(".")))
        if new_v < old_v:
            log(f"[SKIP] Refusing automatic downgrade from {state['current_tag']} to {tag}")
            return
    if state.get("failed_digest") == archive_sha:
        log(f"[SKIP] Previously failed package: {tag}; awaiting a different release")
        return
    log(f"[FOUND] New compatible package: {tag}")
    engine_name, server_name = version_dirs(tag)
    pending = state.get("pending")
    if not (isinstance(pending, dict) and pending.get("tag") == tag and
            pending.get("sha256") == archive_sha and
            (root / engine_name).is_dir() and (root / server_name).is_dir()):
        text = read_checksums(checksums)
        checked_sha = expected_checksum(text, archive["name"])
        if checked_sha != archive_sha:
            raise RuntimeError("SHA256SUMS.txt and GitHub asset digest disagree")
        log(f"[DOWNLOAD] Downloading {archive['name']}")
        file = download_archive(archive, root, checked_sha)
        try:
            log("[EXTRACT] Extracting Engine and Server")
            extract_archive(file, root, tag)
            log("[EXTRACT] Engine and Server ready")
        finally:
            file.unlink(missing_ok=True)
        state["pending"] = {"tag": tag, "sha256": archive_sha}
        save_state(state)
    else:
        validate_component_paths(root / engine_name, root / server_name)
        log("[EXTRACT] Reusing previously prepared Engine and Server")
    log(f"[WAIT] Prepared {tag}; monitoring Strata until safe to install")
    try:
        install_release(root, state, tag, archive_sha, systemctl, api_url)
    except Exception as exc:
        # Never permanently retry a broken build each minute or each scheduled check.
        state["failed_digest"] = archive_sha
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
    signal.signal(signal.SIGUSR1, wake)
    log(f"[START] Updater {VERSION} started; release checks every 12 hours")
    while True:
        try:
            restore_transaction(root, state, systemctl)
            log("[CHECK] Checking GitHub releases")
            _, release, archive, checksums = releases()
            state["last_checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_state(state)
            install_candidate(root, state, release, archive, checksums, systemctl, api_url)
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
WorkingDirectory={systemd_quote(str(base))}
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
        unit_tmp, sudo_tmp = work / "unit", work / "sudoers"
        unit_tmp.write_text(unit, encoding="utf-8")
        sudo_tmp.write_text(rule, encoding="utf-8")
        run_command(["sudo", "-v"], timeout=120)
        # Validate a temporary candidate before installing a privileged policy.
        run_command(["sudo", "/usr/sbin/visudo", "-cf", str(sudo_tmp)])
        sudo_install(sudo_tmp, sudo_file, "0440")
        sudo_install(unit_tmp, unit_file, "0644")
        run_command(["sudo", systemctl, "daemon-reload"])
        run_command(["sudo", systemctl, "enable", "--now", UPDATER])
    log(f"Installed {UPDATER} for user {username}; release root: {base}")


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


def main():
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required")
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    for name in ("install", "uninstall", "run", "status", "check-now", "version"):
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
