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
