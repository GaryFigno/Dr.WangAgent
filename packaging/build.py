"""Build the distributable application.

    python packaging/build.py            # build for this platform
    python packaging/build.py --clean    # remove previous output first

Produces ``dist/Dr.Wang/`` plus a zip beside it. On Windows, pass
``--installer`` to also build an Inno Setup wizard
(``Dr.Wang-*-windows-setup.exe``) when ``iscc`` is on PATH.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC = PROJECT_ROOT / "packaging" / "aiharness.spec"
ISS = PROJECT_ROOT / "packaging" / "drwang.iss"
DIST = PROJECT_ROOT / "dist"
BUILD = PROJECT_ROOT / "build"
APP_NAME = "Dr.Wang"
#: Files copied next to the executable so the bundle is self-explanatory.
DOCS = ("README.md", "LICENSE")


def read_version() -> str:
    """Read the version from the package, without importing it."""
    text = (PROJECT_ROOT / "aiharness" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def ensure_icons() -> None:
    """Regenerate the icon set if it is missing."""
    if (PROJECT_ROOT / "assets" / "icon.ico").exists():
        return
    print("icons missing — generating…")
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "assets" / "build_icons.py")],
        check=True,
        cwd=PROJECT_ROOT,
    )


def clean() -> None:
    for folder in (DIST, BUILD):
        if folder.exists():
            print(f"removing {folder}")
            shutil.rmtree(folder, ignore_errors=True)


def run_pyinstaller() -> Path:
    """Invoke PyInstaller and return the bundle directory.

    Raises:
      SystemExit: If PyInstaller is not installed or the build fails.
    """
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PyInstaller is not installed. Install it with:\n"
            "    pip install pyinstaller"
        ) from None

    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller failed with exit code {result.returncode}")

    bundle = DIST / APP_NAME
    if not bundle.is_dir():
        raise SystemExit(f"expected a bundle at {bundle}, but it is not there")
    return bundle


def add_docs(bundle: Path) -> None:
    for name in DOCS:
        source = PROJECT_ROOT / name
        if source.exists():
            shutil.copy2(source, bundle / name)


def make_archive(bundle: Path, version: str) -> Path:
    """Zip the bundle for distribution."""
    platform = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    archive = DIST / f"{APP_NAME}-{version}-{platform}.zip"
    archive.unlink(missing_ok=True)

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(bundle.parent))
    return archive


def find_iscc() -> Path | None:
    """Locate the Inno Setup compiler on Windows."""
    which = shutil.which("iscc") or shutil.which("ISCC")
    if which:
        return Path(which)
    for candidate in (
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Inno Setup 6"
        / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Inno Setup 6"
        / "ISCC.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def run_installer(version: str) -> Path:
    """Compile the Inno Setup wizard over the PyInstaller bundle."""
    if sys.platform != "win32":
        raise SystemExit("--installer is only supported on Windows")
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit(
            "Inno Setup 6 not found. Install from https://jrsoftware.org/isinfo.php\n"
            "and ensure ISCC.exe is on PATH, then re-run with --installer."
        )
    if not (DIST / APP_NAME / f"{APP_NAME}.exe").is_file():
        raise SystemExit(f"missing bundle exe under {DIST / APP_NAME}")
    if not (PROJECT_ROOT / "LICENSE").is_file():
        raise SystemExit("LICENSE is required for the installer wizard")

    result = subprocess.run(
        [
            str(iscc),
            f"/DMyAppVersion={version}",
            str(ISS),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"ISCC failed with exit code {result.returncode}")

    setup = DIST / f"{APP_NAME}-{version}-windows-setup.exe"
    if not setup.is_file():
        raise SystemExit(f"expected installer at {setup}")
    return setup


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Dr.Wang bundle.")
    parser.add_argument("--clean", action="store_true", help="remove previous output first")
    parser.add_argument("--no-zip", action="store_true", help="skip the archive step")
    parser.add_argument(
        "--installer",
        action="store_true",
        help="also build a Windows Inno Setup wizard (requires ISCC)",
    )
    args = parser.parse_args()

    if args.clean:
        clean()

    version = read_version()
    print(f"building {APP_NAME} {version} for {sys.platform}")
    ensure_icons()

    bundle = run_pyinstaller()
    add_docs(bundle)

    size = sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file())
    print(f"\nbundle: {bundle}  ({size / 1_000_000:.1f} MB)")

    if not args.no_zip:
        archive = make_archive(bundle, version)
        print(f"archive: {archive}  ({archive.stat().st_size / 1_000_000:.1f} MB)")

    if args.installer:
        setup = run_installer(version)
        print(f"installer: {setup}  ({setup.stat().st_size / 1_000_000:.1f} MB)")

    print(
        "\nFirst run:\n"
        f"    {bundle / APP_NAME}\n"
        "Then /setup inside the app to add an account and a model."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
