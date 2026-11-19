#!/usr/bin/env python3
"""
OTA release script: Project Infinity X for Redmi Note 11 (spes).

No `gh` needed. Talks to the GitHub API directly and uploads with curl
(live progress bar, aborts if the upload stalls).

No ROM directory is hardcoded: it searches the server for the newest
Project_Infinity-X zip and reads version / build type / date from it.

Publishes to the OTA repo:
  - spes.json     (what the ROM's ota_url points to)
  - changelog.txt (what the ROM's changelog_url points to)

Usage (python3, NOT bash):
    export GH_TOKEN=github_pat_xxx
    python3 ota.py -m "Changelog text"
    python3 ota.py -m "Changelog" -y
    python3 ota.py --zip /any/path/rom.zip
    python3 ota.py --search-root /mnt/data

Needs: python3, curl, git. Token needs access to the OTA repo
(classic token: "repo" scope; fine-grained: Contents read+write).
"""

import argparse
import base64
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# =========================================================
# CONFIG (things that don't change between servers)
# =========================================================

REPO_OWNER = "sehraan"
OTA_REPO_NAME = "infinity_ota"
REPO = f"{REPO_OWNER}/{OTA_REPO_NAME}"

ROM_NAME = "Project Infinity X"
DEVICE_NAME = "Redmi Note 11"
DEVICE_CODENAME = "spes"

# Must match the URLs in the ROM's overlay XML:
#   https://raw.githubusercontent.com/sehraan/infinity_ota/main/spes.json
#   https://raw.githubusercontent.com/sehraan/infinity_ota/main/changelog.txt
OTA_JSON = f"{DEVICE_CODENAME}.json"
LEGACY_OTA_JSON = "ota-v2.json"   # old name, auto-renamed on first run
CHANGELOG_TXT = "changelog.txt"
DEFAULT_OTA_REPO_PATH = os.path.expanduser("~/infinity_ota_repo")

UPLOAD_TIMEOUT = 3600       # max seconds per file upload attempt
STALL_SECONDS = 120         # abort if speed < 1 KB/s for this long
UPLOAD_ATTEMPTS = 3

ZIP_GLOB = f"Project_Infinity-X-*-{DEVICE_CODENAME}-*.zip"
PRUNE = [".git", ".repo", "proc", "sys", "dev", "node_modules", "ccache"]

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


# =========================================================
# HELPERS
# =========================================================

def die(msg):
    print(f"❌ {msg}")
    sys.exit(1)


def confirm(question, assume_yes):
    if assume_yes:
        return True
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


# =========================================================
# GITHUB API (stdlib only)
# =========================================================

def api(method, path, payload=None):
    """Returns (status_code, parsed_json_or_None)."""
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ota-push-script",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"message": body.decode("utf-8", "ignore")[:200]}
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        die(f"Can't reach api.github.com ({e}). "
            "Check this server's internet / IPv6, then retry.")


def check_token():
    if not TOKEN:
        die("No token. Run:  export GH_TOKEN=your_real_token  and try again.")
    print("🔑 Checking GitHub token...")
    code, data = api("GET", f"/repos/{REPO}")
    if code == 401:
        die("GitHub says the token is invalid or revoked. Make a new one.")
    if code == 404:
        die(f"Repo {REPO} not found, or the token has no access to it.")
    if code != 200:
        die(f"GitHub API error {code}: {data}")
    perms = (data or {}).get("permissions", {})
    if not perms.get("push"):
        die("Token can read the repo but can't write to it. "
            "Needs 'repo' scope (classic) or Contents: Read+write (fine-grained).")
    print("✅ Token OK")


# =========================================================
# FINDING THE ROM ANYWHERE ON THE SERVER
# =========================================================

def quick_candidates():
    here = os.getcwd()
    patterns = [
        os.path.join(here, ZIP_GLOB),
        os.path.join(here, "out", "target", "product", "*", ZIP_GLOB),
        os.path.join(here, "*", "out", "target", "product", "*", ZIP_GLOB),
    ]
    found = []
    for p in patterns:
        found += glob.glob(p)
    return found


def find_command(root):
    cmd = ["find", root, "-xdev"]
    for d in PRUNE:
        cmd += ["-name", d, "-prune", "-o"]
    cmd += ["-type", "f", "-name", ZIP_GLOB, "-print"]
    return cmd


def search_server(roots):
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        print(f"   searching {root} ...")
        result = subprocess.run(find_command(root), capture_output=True, text=True)
        found += [l for l in result.stdout.splitlines() if l.strip()]
        if found:
            break
    return found


def find_zips(search_roots):
    found = quick_candidates()
    if not found:
        roots = search_roots or ["/mnt", "/home", "/data", "/root", "/"]
        found = search_server(roots)
    found = list(dict.fromkeys(os.path.abspath(f) for f in found))
    found.sort(key=os.path.getmtime, reverse=True)
    return found


# =========================================================
# READING BUILD INFO FROM THE ZIP
# =========================================================

def parse_zip_name(path):
    name = os.path.basename(path)
    m = re.match(
        r"Project_Infinity-X-([\d.]+)-([A-Za-z0-9_]+)-"
        r"(\d{2}\.\d{2}\.\d{4})-([A-Za-z]+)",
        name,
    )
    if not m:
        return None
    return {
        "version": m.group(1),
        "codename": m.group(2),
        "date": m.group(3),
        "type": m.group(4).upper(),
    }


def read_zip_metadata(path):
    try:
        with zipfile.ZipFile(path) as z:
            text = z.read("META-INF/com/android/metadata").decode("utf-8", "ignore")
    except Exception:
        return {}
    info = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()
    return info


def get_build_info(path):
    info = parse_zip_name(path)
    meta = read_zip_metadata(path)
    if not info:
        version = re.search(r"(\d+\.\d+(?:\.\d+)?)", os.path.basename(path))
        info = {
            "version": version.group(1) if version else "unknown",
            "codename": meta.get("pre-device", DEVICE_CODENAME).split(",")[0],
            "date": time.strftime("%d.%m.%Y", time.localtime(os.path.getmtime(path))),
            "type": "GAPPS" if "GAPPS" in path.upper() else "VANILLA",
        }
    info["sdk"] = meta.get("post-sdk-level", "")
    return info


def get_file_info(path):
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest(), os.path.getsize(path), int(os.path.getmtime(path))


def find_boot_img(zip_path):
    folder = os.path.dirname(zip_path)
    for name in ("boot.img", "init_boot.img"):
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            return p
    return None


# =========================================================
# RELEASES + UPLOAD (API + curl)
# =========================================================

def find_releases_with_tag(tag):
    """Includes drafts (e.g. leftovers from a killed upload)."""
    code, data = api("GET", f"/repos/{REPO}/releases?per_page=100")
    if code != 200 or not isinstance(data, list):
        return []
    return [r for r in data if r.get("tag_name") == tag]


def delete_release(release):
    api("DELETE", f"/repos/{REPO}/releases/{release['id']}")
    if not release.get("draft"):
        api("DELETE", f"/repos/{REPO}/git/refs/tags/{release['tag_name']}")


def upload_asset(release_id, path, auth_header_file):
    name = os.path.basename(path)
    size_mb = os.path.getsize(path) // (1024 * 1024)
    url = (
        f"https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets"
        f"?name={urllib.parse.quote(name)}"
    )
    resp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json").name
    print(f"   ⬆️  {name} ({size_mb} MB)")
    cmd = [
        "curl", "-#", "-X", "POST",
        "-H", f"@{auth_header_file}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "Content-Type: application/octet-stream",
        "-T", path,
        "--connect-timeout", "30",
        "--speed-limit", "1024", "--speed-time", str(STALL_SECONDS),
        "--max-time", str(UPLOAD_TIMEOUT),
        "-o", resp_file,
        "-w", "%{http_code}",
        url,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    status = result.stdout.strip()
    try:
        body = open(resp_file).read()[:300]
    except Exception:
        body = ""
    os.unlink(resp_file)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed (exit {result.returncode}), "
                           "upload stalled or connection dropped")
    if status != "201":
        raise RuntimeError(f"GitHub returned HTTP {status}: {body}")


def publish_release(tag, info, files, changelog):
    notes = (
        f"{ROM_NAME} {info['version']} for {DEVICE_NAME} ({info['codename']})\n"
        f"Release type: {info['type']}\n"
        f"Build date: {info['date']}\n\n"
        f"{changelog}"
    )

    # token goes in a private temp file so it isn't visible in `ps`
    fd, header_file = tempfile.mkstemp(suffix=".hdr")
    os.chmod(header_file, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"Authorization: Bearer {TOKEN}\n")

    try:
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            print(f"📦 Attempt {attempt}/{UPLOAD_ATTEMPTS}: creating draft release...")
            code, rel = api("POST", f"/repos/{REPO}/releases", {
                "tag_name": tag,
                "name": f"{ROM_NAME} {info['version']} - {DEVICE_NAME}",
                "body": notes,
                "draft": True,
            })
            if code != 201:
                die(f"Could not create release ({code}): {rel}")
            try:
                for path in files:
                    upload_asset(rel["id"], path, header_file)
                code, done = api("PATCH", f"/repos/{REPO}/releases/{rel['id']}",
                                 {"draft": False})
                if code != 200:
                    raise RuntimeError(f"Could not publish release ({code}): {done}")
                print("✅ Upload finished and release published")
                return
            except RuntimeError as e:
                print(f"\n❌ Attempt {attempt} failed: {e}")
                delete_release(rel)
                if attempt == UPLOAD_ATTEMPTS:
                    die("All upload attempts failed.")
                time.sleep(10)
    finally:
        os.unlink(header_file)


# =========================================================
# OTA JSON + CHANGELOG
# =========================================================

FIELD_ALIASES = {
    "filename": ["filename", "file_name"],
    "url": ["url", "download", "download_url"],
    "size": ["size"],
    "sha256": ["sha256", "id"],
    "timestamp": ["datetime", "timestamp"],
    "version": ["version"],
    "release_type": ["romtype", "buildtype", "type"],
    "changelog": ["changelog"],
}


def set_field(entry, field, value):
    aliases = FIELD_ALIASES[field]
    present = [k for k in aliases if k in entry]
    if present:
        for k in present:
            entry[k] = value
    else:
        entry[aliases[0]] = value


def update_ota_json(json_path, values):
    if os.path.exists(json_path):
        with open(json_path) as f:
            data = json.load(f)
    else:
        data = {"response": [{}]}

    if isinstance(data, dict) and isinstance(data.get("response"), list):
        if not data["response"]:
            data["response"].append({})
        entry = data["response"][0]
    elif isinstance(data, dict):
        entry = data
    else:
        die(f"Unexpected format in {json_path}")

    for field, value in values.items():
        set_field(entry, field, value)

    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def write_changelog(path, info, changelog):
    text = (
        f"{ROM_NAME} {info['version']} ({info['type']}) - {info['date']}\n\n"
        f"{changelog}\n"
    )
    with open(path, "w") as f:
        f.write(text)


# =========================================================
# GIT (plain git + token header, no gh)
# =========================================================

def git(args, cwd=None, check=True):
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    })
    return subprocess.run(
        ["git"] + args, cwd=cwd, env=env, check=check, timeout=300
    )


def ensure_ota_repo(path):
    if not os.path.isdir(os.path.join(path, ".git")):
        print(f"📥 Cloning {REPO} to {path}...")
        git(["clone", f"https://github.com/{REPO}.git", path])
    git(["checkout", "main"], cwd=path)
    git(["pull", "--rebase"], cwd=path)


def migrate_legacy_json(path):
    """Rename ota-v2.json -> spes.json once, keeping its history/fields."""
    new = os.path.join(path, OTA_JSON)
    old = os.path.join(path, LEGACY_OTA_JSON)
    if not os.path.exists(new) and os.path.exists(old):
        print(f"🔁 Renaming {LEGACY_OTA_JSON} -> {OTA_JSON}")
        git(["mv", LEGACY_OTA_JSON, OTA_JSON], cwd=path)


def push_json(path, tag):
    git(["add", OTA_JSON, CHANGELOG_TXT], cwd=path)
    commit = git(
        ["-c", f"user.name={REPO_OWNER}",
         "-c", f"user.email={REPO_OWNER}@users.noreply.github.com",
         "commit", "-m", f"OTA update: {tag}"],
        cwd=path, check=False,
    )
    if commit.returncode != 0:
        print("ℹ️ Nothing new to commit.")
    git(["push", "origin", "main"], cwd=path)


# =========================================================
# MAIN
# =========================================================

def upload_and_push():
    parser = argparse.ArgumentParser(
        description=f"Publish {ROM_NAME} OTA for {DEVICE_NAME}"
    )
    parser.add_argument("-m", "--message", default="", help="changelog text")
    parser.add_argument("--changelog-file", default=None,
                        help="read changelog from a file")
    parser.add_argument("--zip", dest="zip_path", default=None,
                        help="exact zip path (skips searching)")
    parser.add_argument("--search-root", action="append", default=[],
                        help="folder to search (repeatable)")
    parser.add_argument("--ota-repo", default=DEFAULT_OTA_REPO_PATH,
                        help="where to keep the local OTA repo clone")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="no confirmation prompts")
    args = parser.parse_args()

    check_token()

    # ---------- find the zip ----------
    if args.zip_path:
        zip_path = os.path.abspath(args.zip_path)
        if not os.path.isfile(zip_path):
            die(f"Zip not found: {zip_path}")
    else:
        print(f"🔎 Searching this server for a {DEVICE_CODENAME} build...")
        zips = find_zips(args.search_root)
        if not zips:
            die("No Project_Infinity-X zip found. "
                "Use --zip /path/to/rom.zip or --search-root /folder")
        print("Found (newest first):")
        for i, z in enumerate(zips[:10], 1):
            print(f"  {i}. {z}")
        zip_path = zips[0]
        if len(zips) > 1 and not args.yes:
            pick = input(f"Which one? [1-{min(len(zips), 10)}, Enter = newest] ").strip()
            if pick.isdigit() and 1 <= int(pick) <= min(len(zips), 10):
                zip_path = zips[int(pick) - 1]

    filename = os.path.basename(zip_path)
    info = get_build_info(zip_path)
    boot_path = find_boot_img(zip_path)
    size_mb = os.path.getsize(zip_path) // (1024 * 1024)
    tag = f"{info['version']}-{info['date']}-{info['type']}"

    changelog = args.message
    if args.changelog_file:
        with open(args.changelog_file) as f:
            changelog = f.read().strip()
    changelog = changelog or info["version"]

    print()
    print(f"Zip:        {zip_path} ({size_mb} MB)")
    print(f"Version:    {info['version']}  ({info['type']}, built {info['date']})")
    print(f"Device:     {DEVICE_NAME} ({info['codename']})")
    print(f"Boot image: {boot_path or 'not found, skipping'}")
    print(f"Tag:        {tag}")
    print(f"To repo:    {REPO}  ({OTA_JSON}, {CHANGELOG_TXT})")
    print(f"Changelog:  {changelog}")
    print()

    if not confirm("Is this the right build?", args.yes):
        die("Cancelled.")

    # ---------- leftovers with the same tag (incl. drafts) ----------
    old = find_releases_with_tag(tag)
    if old:
        print(f"⚠️  {len(old)} existing release(s) with tag {tag} "
              "(may be leftovers from a failed upload).")
        if not confirm("Delete them and re-upload?", args.yes):
            die("Cancelled.")
        for r in old:
            delete_release(r)

    files = [zip_path] + ([boot_path] if boot_path else [])
    publish_release(tag, info, files, changelog)

    download_url = f"https://github.com/{REPO}/releases/download/{tag}/{filename}"

    # ---------- OTA JSON + changelog ----------
    print("🧮 Calculating checksum...")
    sha256, size, timestamp = get_file_info(zip_path)

    ensure_ota_repo(args.ota_repo)
    migrate_legacy_json(args.ota_repo)

    update_ota_json(
        os.path.join(args.ota_repo, OTA_JSON),
        {
            "filename": filename,
            "url": download_url,
            "size": size,
            "sha256": sha256,
            "timestamp": timestamp,
            "version": info["version"],
            "release_type": info["type"],
            "changelog": changelog,
        },
    )
    write_changelog(os.path.join(args.ota_repo, CHANGELOG_TXT), info, changelog)

    print("📤 Pushing OTA JSON + changelog...")
    push_json(args.ota_repo, tag)

    print("✅ OTA update complete!")
    print(f"   {download_url}")


if __name__ == "__main__":
    upload_and_push()
    
