#!/usr/bin/env python3
"""
manifest.py — integrity manifest for the LP-Flow Monitor frozen artifact (v3).

The manifest is a canonical JSON object mapping each frozen file (relative, forward-slash path)
to the SHA-256 of its RAW BYTES, plus the schema_version. The expected digest of the canonical
manifest is stored EXTERNALLY in MANIFEST.sha256, which is NOT part of the manifest — this avoids
a self-referential digest (freezing writes only the external file, changing nothing that is hashed).

verify_integrity() recomputes the manifest from disk, hashes its canonical JSON, and compares to
MANIFEST.sha256. Any change to a frozen file changes the digest and makes the check fail.

CLAIM SCOPE (read carefully): MANIFEST.sha256 lives in the repository. This proves the artifact is
INTEGRITY-CHECKED AGAINST A PINNED MANIFEST — accidental drift, partial edits, or a single-file
change are caught. It is NOT tamper-proof: a coordinated edit of both the code and MANIFEST.sha256
would pass. Tamper resistance requires an external signed anchor (a signed release/tag), which is a
separate, future step.
"""
from __future__ import annotations

import hashlib
import json
import os

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
SCHEMA_VERSION = "3.0.1"

# Frozen files (relative to repo root). pools.json is intentionally EXCLUDED — it is dynamic
# (monthly TVL re-rank); each run records its own config_digest instead. Tests and data are not
# part of the behavioural artifact and are excluded.
MANIFEST_FILES = [
    "detect_monitoring_v3.py",
    "examples/run_detector_on_csv.py",
    "sql/lp_flow_template.sql",
    "schema.json",
    "requirements.lock",
    "manifest.py",
    "scripts/build_v3_snapshot.py",
]

# External anchor — deliberately NOT in MANIFEST_FILES (avoids a self-referential digest).
DIGEST_FILE = "MANIFEST.sha256"


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def file_sha256(relpath: str) -> str:
    with open(os.path.join(REPO_ROOT, relpath), "rb") as fh:
        return _sha256_bytes(fh.read())


def compute_manifest() -> dict:
    files = {rel.replace(os.sep, "/"): file_sha256(rel) for rel in MANIFEST_FILES}
    return {"schema_version": SCHEMA_VERSION, "files": dict(sorted(files.items()))}


def canonical_manifest_json(manifest: dict | None = None) -> str:
    return json.dumps(manifest or compute_manifest(), sort_keys=True, separators=(",", ":"))


def compute_manifest_digest() -> str:
    return _sha256_bytes(canonical_manifest_json().encode("utf-8"))


def read_expected_digest() -> str | None:
    path = os.path.join(REPO_ROOT, DIGEST_FILE)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        line = fh.readline().strip()
    return line.split()[0] if line else None


def verify_integrity() -> None:
    """Fail-closed integrity check for the frozen artifact. The external digest anchor is
    MANDATORY: a missing, malformed, or mismatched MANIFEST.sha256 is a terminal error."""
    path = os.path.join(REPO_ROOT, DIGEST_FILE)
    if not os.path.exists(path):
        raise RuntimeError(
            f"Integrity check FAILED: {DIGEST_FILE} is missing. A frozen release must ship its "
            "digest anchor. Refusing to run."
        )
    expected = read_expected_digest()
    if not expected or len(expected) != 64:
        raise RuntimeError(
            f"Integrity check FAILED: {DIGEST_FILE} is empty or malformed. Refusing to run."
        )
    actual = compute_manifest_digest()
    if actual != expected:
        raise RuntimeError(
            f"Integrity check FAILED: manifest digest {actual} != expected {expected} "
            f"(from {DIGEST_FILE}). A frozen file was modified. Refusing to run."
        )


def config_digest(config_path: str | None) -> str | None:
    """SHA-256 of a (dynamic) config file's raw bytes, e.g. pools.json. None if absent."""
    if not config_path or not os.path.exists(config_path):
        return None
    with open(config_path, "rb") as fh:
        return _sha256_bytes(fh.read())


def freeze(write: bool = False) -> str:
    """Compute the manifest digest and (optionally) write it to the external MANIFEST.sha256."""
    digest = compute_manifest_digest()
    if write:
        with open(os.path.join(REPO_ROOT, DIGEST_FILE), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{digest}  lp-flow-monitor manifest; schema_version={SCHEMA_VERSION}\n")
    return digest


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["freeze"]:
        print("FROZE manifest digest:", freeze(write=True))
    elif sys.argv[1:2] == ["show"]:
        print(canonical_manifest_json())
        print("digest:  ", compute_manifest_digest())
        print("expected:", read_expected_digest())
    else:
        try:
            verify_integrity()
        except RuntimeError as e:
            print("INTEGRITY: FAIL —", e)
            sys.exit(1)
        print("INTEGRITY: OK — manifest digest matches", DIGEST_FILE)
        print("digest:", compute_manifest_digest())
