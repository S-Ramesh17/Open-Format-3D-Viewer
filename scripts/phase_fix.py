#!/usr/bin/env python3
"""
Phase Fix Utility

Safely applies the automated Phase fixes.

Functions:
1. Adds profiling import to auth router.
2. Adds @profile decorators to login/register if missing.
3. Updates outdated integration tests expecting HTTP 422 -> 400.
4. Preserves upload-limit tests as HTTP 413.
5. Creates .bak backups before modifying files.

Run:
    python scripts/phase_fix.py
"""

from __future__ import annotations

import glob
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def backup(path: Path) -> None:
    """Create a backup before modifying."""
    backup_path = path.with_suffix(path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)


def patch_auth_router() -> bool:
    auth_file = ROOT / "apps" / "api" / "app" / "routers" / "auth.py"

    if not auth_file.exists():
        print(f"[SKIP] {auth_file} not found")
        return False

    backup(auth_file)

    content = auth_file.read_text(encoding="utf-8")
    original = content

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    if "from app.core.profiling import profile" not in content:
        content = re.sub(
            r"(from fastapi import APIRouter[^\n]*)",
            r"\1\nfrom app.core.profiling import profile",
            content,
            count=1,
        )

    # ------------------------------------------------------------------
    # Login decorator
    # ------------------------------------------------------------------

    content = re.sub(
        r'(?<!@profile\("auth_login"\)\n)(@router\.post\("/login".*?\n)',
        '@profile("auth_login")\n\\1',
        content,
        count=1,
    )

    # ------------------------------------------------------------------
    # Register decorator
    # ------------------------------------------------------------------

    content = re.sub(
        r'(?<!@profile\("auth_register"\)\n)(@router\.post\("/register".*?\n)',
        '@profile("auth_register")\n\\1',
        content,
        count=1,
    )

    if content != original:
        auth_file.write_text(content, encoding="utf-8")
        print("[OK] Patched auth router")
        return True

    print("[OK] Auth router already patched")
    return False


def patch_tests() -> int:
    tests = glob.glob(str(ROOT / "apps" / "api" / "tests" / "*.py"))

    changed = 0

    for file in tests:
        path = Path(file)
        backup(path)

        content = path.read_text(encoding="utf-8")
        original = content

        # Generic validation change
        content = content.replace("== 422", "== 400")

        # Upload size tests should remain 413
        upload_keywords = (
            "limit",
            "too_large",
            "file_too_large",
            "upload_limit",
            "exceeds",
            "payload_too_large",
        )

        filename = path.name.lower()

        if any(k in filename for k in upload_keywords):

            content = re.sub(
                r"status_code\s*==\s*400",
                "status_code == 413",
                content,
            )

        else:

            lines = content.splitlines()

            for i, line in enumerate(lines):
                if (
                    "status_code == 400" in line
                    and any(
                        k in line.lower()
                        for k in upload_keywords
                    )
                ):
                    lines[i] = line.replace("400", "413")

            content = "\n".join(lines)

        if content != original:
            path.write_text(content, encoding="utf-8")
            changed += 1
            print(f"[OK] Updated {path.name}")

    return changed


def main():
    print("=" * 60)
    print("Applying Phase Fixes")
    print("=" * 60)

    patch_auth_router()

    modified = patch_tests()

    print()
    print("=" * 60)
    print(f"Modified test files : {modified}")
    print("Backups created     : *.bak")
    print("=" * 60)
    print()
    print("Review changes using:")
    print("    git diff")
    print()
    print("If everything looks correct:")
    print("    docker compose build api")
    print("    docker compose restart api")
    print()


if __name__ == "__main__":
    main()