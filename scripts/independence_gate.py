#!/usr/bin/env python3
"""Independence acceptance gate (docs/spec/05 §5).

Fails (exit 1) if any LiteLLM artefact is found in: pyproject.toml, lockfiles, the installed environment,
an optional image file list, or an optional CycloneDX/SPDX SBOM. Usage:
    python scripts/independence_gate.py [--image-files files.txt] [--sbom sbom.json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

PATTERN = re.compile(r"litellm", re.IGNORECASE)            # lockfiles, freeze, image layers, SBOM: any mention
DEP_PATTERN = re.compile(r"""^\s*["']litellm""", re.IGNORECASE)   # pyproject/package.json dependency entries
CODE_PATTERN = re.compile(r"^\s*(import|from)\s+litellm\b")     # source imports (prose mentions are allowed)
ROOT = pathlib.Path(__file__).resolve().parents[1]


def scan_text(label: str, text: str, pattern: re.Pattern = PATTERN) -> list[str]:
    return [f"{label}: line {i + 1}: {line.strip()[:120]}" for i, line in enumerate(text.splitlines()) if pattern.search(line)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-files")
    ap.add_argument("--sbom")
    args = ap.parse_args()
    hits: list[str] = []
    for name in ("pyproject.toml", "package.json", "portal/package.json"):
        p = ROOT / name
        if p.exists():
            hits += scan_text(name, p.read_text(errors="replace"), DEP_PATTERN)
    for name in ("uv.lock", "poetry.lock", "requirements.txt", "requirements.lock", "package-lock.json",
                 "pnpm-lock.yaml", "portal/package-lock.json"):
        p = ROOT / name
        if p.exists():
            hits += scan_text(name, p.read_text(errors="replace"))
    for p in (ROOT / "src").rglob("*.py"):
        hits += scan_text(str(p.relative_to(ROOT)), p.read_text(errors="replace"), CODE_PATTERN)
    freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True).stdout
    hits += scan_text("pip freeze", freeze)
    if args.image_files:
        hits += scan_text("image layers", pathlib.Path(args.image_files).read_text(errors="replace"))
    if args.sbom:
        data = json.loads(pathlib.Path(args.sbom).read_text())
        comps = data.get("components") or data.get("packages") or []
        for c in comps:
            if PATTERN.search(json.dumps(c)):
                hits.append(f"sbom: {c.get('name')} {c.get('version', '')}")
    if hits:
        print("INDEPENDENCE GATE FAILED — LiteLLM artefacts found:")
        print("\n".join("  " + h for h in hits))
        return 1
    print("independence gate: OK (no LiteLLM package, code, or image artefact)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
