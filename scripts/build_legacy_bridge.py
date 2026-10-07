#!/usr/bin/env python3
"""Derive Chrome 59 bridge assets from the unmodified pinned upstream Core.

Regenerate with --esbuild /path/to/esbuild (exactly 0.27.2). --check verifies the
checked-in provenance and all bytes without acquiring a tool. Supplying the tool
with --check additionally verifies a fresh transformation is byte-identical.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "Resources/legacy"
UPSTREAM = ROOT / "ThirdParty/AuroraViewCore"
FILES = ("event_bridge.js", "bridge_stub.js")
VERSION = "0.27.2"
OPTIONS = ("--target=chrome59", "--format=iife", "--charset=utf8", "--legal-comments=inline")
GENERATOR = {"name": "esbuild", "version": VERSION, "target": "chrome59", "options": list(OPTIONS)}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_identity() -> tuple[dict, dict[str, bytes]]:
    manifest_bytes = (UPSTREAM / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    sources = {}
    for name in FILES:
        data = (UPSTREAM / name).read_bytes()
        if digest(data) != manifest["files"][name]["sha256"]:
            raise ValueError(f"Pinned upstream asset changed: {name}")
        sources[name] = data
    return {
        "repository": manifest["repository"],
        "commit": manifest["commit"],
        "manifest_sha256": digest(manifest_bytes),
    }, sources


def transform(executable: str, source: bytes) -> bytes:
    result = subprocess.run([executable, *OPTIONS], input=source, capture_output=True, check=True)
    if not result.stdout or b"\r" in result.stdout:
        raise ValueError("Expected nonempty deterministic LF output from esbuild")
    return result.stdout


def checked_tool(value: str | None) -> str:
    executable = value or shutil.which("esbuild")
    if not executable:
        raise ValueError(f"Regeneration requires esbuild {VERSION}; pass --esbuild with its executable path")
    version = subprocess.run([executable, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if version != VERSION:
        raise ValueError(f"Expected esbuild {VERSION}, got {version}")
    return executable


def check(executable: str | None = None) -> None:
    upstream, sources = source_identity()
    manifest = json.loads((OUTPUT / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("generator") != GENERATOR or manifest.get("upstream") != upstream:
        raise ValueError("Legacy bridge provenance does not match the pinned generator and upstream source")
    if set(manifest.get("files", {})) != set(FILES):
        raise ValueError("Legacy bridge file set does not match the bridge contract")
    for name, source in sources.items():
        output = (OUTPUT / name).read_bytes()
        expected = {"source": f"ThirdParty/AuroraViewCore/{name}", "source_sha256": digest(source), "sha256": digest(output)}
        if not output or b"\r" in output or manifest["files"][name] != expected:
            raise ValueError(f"Legacy bridge bytes or source identity changed: {name}")
        if executable and transform(executable, source) != output:
            raise ValueError(f"Legacy bridge is not reproducible: {name}")


def generate(executable: str) -> None:
    upstream, sources = source_identity()
    outputs = {name: transform(executable, source) for name, source in sources.items()}
    manifest = {"schema_version": 1, "generator": GENERATOR, "upstream": upstream, "files": {}}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, output in outputs.items():
        (OUTPUT / name).write_bytes(output)
        manifest["files"][name] = {
            "source": f"ThirdParty/AuroraViewCore/{name}",
            "source_sha256": digest(sources[name]),
            "sha256": digest(output),
        }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--esbuild", help=f"Path to the esbuild {VERSION} executable")
    parser.add_argument("--check", action="store_true", help="Verify existing assets; do not write files")
    args = parser.parse_args()
    tool = checked_tool(args.esbuild) if args.esbuild or not args.check else None
    if args.check:
        check(tool)
    else:
        generate(tool)
        check(tool)
    print(json.dumps({"status": "pass", "target": "chrome59", "generator": "esbuild", "version": VERSION, "files": list(FILES)}))


if __name__ == "__main__":
    main()
