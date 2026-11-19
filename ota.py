#!/usr/bin/env python3
"""
OTA release script: Project Infinity X for Redmi Note 11 (spes).

Works on any server: no ROM directory is hardcoded. It searches the
server for the newest Project_Infinity-X zip and reads the version,
build type, date and device codename from the zip itself.

Usage (python3, NOT bash):
    export GH_TOKEN=github_pat_xxx
    python3 ota_push.py -m "Changelog text"
    python3 ota_push.py -m "Changelog" -y
    python3 ota_push.py --zip /any/path/rom.zip
    python3 ota_push.py --search-root /mnt/data     # limit where it looks
"""

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile

# =========================================================
# CONFIG (only things that don't change between servers)
# =========================================================

REPO_OWNER = "sehraan"
OTA_REPO_NAME = "infinity_ota"
REPO = f"{REPO_OWNER}/{OTA_REPO_NAME}"

ROM_NAME = "Project Infinity X"
DEVICE_NAME = "Redmi Note 11"
DEVICE_CODENAME = "spes"

OTA_JSON = "ota-v2.json"

# Local clone of the OTA repo; created automatically, any server.
DEFAULT_OTA_REPO_PATH = os.path.expanduser("~/infinity_ota_repo")

UPLOAD_TIMEOUT = 3600   # seconds per attempt (60 min)
UPLOAD_ATTEMPTS = 3

ZIP_GLOB = f"Project_Infinity-X-*-{DEVICE_CODENAME}-*.zip"

# folders never worth searching
PRUNE = [".git", ".repo", "proc", "sys", "dev", "node_modules", "ccache"]


# =========================================================
# HELPERS
# =========================================================

def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def die(msg):
    print(f"❌ {msg}")
    sys.exit(1)


def confirm(question, assume_yes):
    if assume_yes:
        return True
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


def check_gh_auth():
    result = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True
    )
    if result.returncode != 0:
        die(
            "gh is not logged in. Run:  export GH_TOKEN=your_token  "
            "then try again.\n" + (result.stderr or result.stdout).strip()
        )


# =========================================================
# FINDING THE ROM ANYWHERE ON THE SERVER
# =========================================================

def quick_candidates():
    """Cheap checks first: current dir and its out/ folders."""
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
        result = subprocess.run(
            find_command(root), capture_output=True, text=True
        )
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
    """
    Project_Infinity-X-4.2-spes-19.11.2026-VANILLA-UNOFFICIAL.zip
    -> version 4.2, codename spes, date 19.11.2026, type VANILLA
    """
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
    """Fallback: Android OTA metadata inside the zip."""
    try:
        with zipfile.ZipFile(path) as z:
            text = z.read("META-INF/com/android/metadata").decode(
                "utf-8", "ignore"
            )
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
        # name didn't match: fall back on whatever we can find
        version = re.search(r"(\d+\.\d+(?:\.\d+)?)", os.path.basename(path))
        info = {
            "version": version.group(1) if version else "unknown",
            "codename": meta.get("pre-device", DEVICE_CODENAME).split(",")[0],
            "date": time.strftime("%d.%m.%Y", time.localtime(os.path.getmtime(path))),
            "type": "VANILLA",
        }
        if "GAPPS" in path.upper():
            info["type"] = "GAPPS"
    # Android SDK level, if present in the zip metadata
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
# UPLOAD
# =========================================================

def release_exists(tag):
    r = subprocess.run(
        ["gh", "release", "view", tag, "--repo", REPO], capture_output=True
    )
    return r.returncode == 0


def delete_release(tag):
    subprocess.run(
        ["gh", "release", "delete", tag, "--repo", REPO,
         "--yes", "--cleanup-tag"]
    )


def upload_release(tag, info, zip_path, boot_path, changelog):
    notes = (
        f"{ROM_NAME} {info['version']} for {DEVICE_NAME} ({info['codename']})\n"
        f"Release type: {info['type']}\n"
        f"Build date: {info['date']}\n\n"
        f"{changelog}"
    )

    cmd = ["gh", "release", "create", tag, zip_path]
    if boot_path:
        cmd.append(boot_path)
    cmd += [
        "--repo", REPO,
        "--title", f"{ROM_NAME} {info['version']} - {DEVICE_NAME}",
        "--notes", notes,
    ]

    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        print(f"⬆️  Upload attempt {attempt}/{UPLOAD_ATTEMPTS} "
              f"(timeout {UPLOAD_TIMEOUT // 60} min)...")
        try:
            subprocess.run(cmd, check=True, timeout=UPLOAD_TIMEOUT)
            print("✅ Upload finished")
            return
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            print(f"❌ Upload attempt {attempt} failed: {type(e).__name__}")
            delete_release(tag)
            if attempt == UPLOAD_ATTEMPTS:
                die("All upload attempts failed.")
            time.sleep(10)


# =========================================================
# OTA JSON
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


# =========================================================
# GIT
# =========================================================

def ensure_ota_repo(path):
    run(["gh", "auth", "setup-git"])
    if not os.path.isdir(os.path.join(path, ".git")):
        print(f"📥 Cloning {REPO} to {path}...")
        run(["gh", "repo", "clone", REPO, path])
    run(["git", "checkout", "main"], cwd=path)
    run(["git", "pull", "--rebase"], cwd=path)


def push_json(path, tag):
    run(["git", "add", OTA_JSON], cwd=path)
    commit = subprocess.run(
        ["git",
         "-c", f"user.name={REPO_OWNER}",
         "-c", f"user.email={REPO_OWNER}@users.noreply.github.com",
         "commit", "-m", f"OTA update: {tag}"],
        cwd=path,
    )
    if commit.returncode != 0:
        print("ℹ️ Nothing new to commit.")
    run(["git", "push", "origin", "main"], cwd=path)


# =========================================================
# MAIN
# =========================================================

def upload_and_push():
    parser = argparse.ArgumentParser(
        description=f"Publish {ROM_NAME} OTA for {DEVICE_NAME}"
    )
    parser.add_argument("-m", "--message", default="",
                        help="changelog text")
    parser.add_argument("--changelog-file", default=None,
                        help="read changelog from a file")
    parser.add_argument("--zip", dest="zip_path", default=None,
                        help="exact zip path (skips searching)")
    parser.add_argument("--search-root", action="append", default=[],
                        help="folder to search (repeatable); "
                             "default: current dir, then /mnt /home /data /root /")
    parser.add_argument("--ota-repo", default=DEFAULT_OTA_REPO_PATH,
                        help="where to keep the local OTA repo clone")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="no confirmation prompts")
    args = parser.parse_args()

    check_gh_auth()

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
            pick = input(f"Which one? [1-{min(len(zips), 10)}, "
                         f"Enter = newest] ").strip()
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
    print(f"To repo:    {REPO}")
    print(f"Changelog:  {changelog}")
    print()

    if not confirm("Is this the right build?", args.yes):
        die("Cancelled.")

    if release_exists(tag):
        print(f"⚠️  A release with tag {tag} already exists.")
        if not confirm("Delete it and re-upload?", args.yes):
            die("Cancelled.")
        delete_release(tag)

    upload_release(tag, info, zip_path, boot_path, changelog)

    download_url = f"https://github.com/{REPO}/releases/download/{tag}/{filename}"

    print("🧮 Calculating checksum...")
    sha256, size, timestamp = get_file_info(zip_path)

    ensure_ota_repo(args.ota_repo)

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

    print("📤 Pushing OTA JSON...")
    push_json(args.ota_repo, tag)

    print("✅ OTA update complete!")
    print(f"   {download_url}")


if __name__ == "__main__":
    upload_and_push()
