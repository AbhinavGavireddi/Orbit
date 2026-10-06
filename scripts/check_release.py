"""Release hygiene checks for the publishable repository surface.

The checker intentionally skips ignored files, including .env, dist, var and
local docs. It is not a secret scanner replacement; it catches common local
breadcrumbs before a release.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".c", ".css", ".h", ".hcl", ".html", ".json", ".md", ".plist", ".py",
    ".sh", ".swift", ".toml", ".txt", ".yaml", ".yml",
}
REQUIRED = {
    "README.md",
    "DEPLOYMENT.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    ".env.example",
    ".env.prod.example",
    "compose.yaml",
    "compose.prod.yaml",
    "deploy/Caddyfile",
}
FORBIDDEN = {
    "Ab" + "hinav": "personal name",
    "/Us" + "ers/": "absolute user path",
    "Mac" + "Book": "local host reference",
}
SKIP_DIRS = {".git", ".venv", "dist", "var", "__pycache__", ".pytest_cache", ".ruff_cache", ".build", ".build-native"}


def ignored(path: Path) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(path.relative_to(ROOT))],
        cwd=ROOT,
        check=False,
    )
    return result.returncode == 0


def publishable_files():
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_dir() or ignored(path):
            continue
        yield path


def text_file(path: Path) -> bool:
    return path.name in {"LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"} or path.suffix in TEXT_SUFFIXES


def main() -> int:
    failures = []
    present = {str(path.relative_to(ROOT)) for path in publishable_files()}
    for required in sorted(REQUIRED):
        if required not in present:
            failures.append(f"missing required release file: {required}")
    readmes = sorted(path for path in present if path.endswith("README.md"))
    if readmes != ["README.md"]:
        failures.append("expected exactly one publishable README.md, found: " + ", ".join(readmes))
    for path in publishable_files():
        if not text_file(path):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError as error:
            failures.append(f"cannot read {path.relative_to(ROOT)}: {error}")
            continue
        for needle, reason in FORBIDDEN.items():
            if needle in text:
                failures.append(f"{path.relative_to(ROOT)} contains {reason}: {needle}")
    if failures:
        print("Release check failed:")
        for failure in failures:
            print("- " + failure)
        return 1
    print("Release check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
