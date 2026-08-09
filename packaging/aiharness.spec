# PyInstaller build spec for Dr.Wang.
#
#     pip install "aiharness[dev]" pyinstaller
#     pyinstaller packaging/aiharness.spec --noconfirm
#
# Produces dist/Dr.Wang/ — a one-directory bundle. One-file mode is
# deliberately not used: it unpacks to a temp directory on every launch, which
# costs a second of startup and confuses antivirus software on Windows.
#
# Textual's CSS and the icon assets are data files, not code, so they have to
# be listed explicitly; PyInstaller cannot see them by import analysis.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent
APP_NAME = "Dr.Wang"

datas = [
    (str(PROJECT_ROOT / "aiharness" / "ui" / "styles.tcss"), "aiharness/ui"),
    (str(PROJECT_ROOT / "assets" / "icon.svg"), "assets"),
    (str(PROJECT_ROOT / "assets" / "icon-256.png"), "assets"),
    # Tray sizes. Without these the packaged tray falls back to a drawn
    # placeholder instead of the actual icon.
    (str(PROJECT_ROOT / "assets" / "icon-64.png"), "assets"),
    (str(PROJECT_ROOT / "assets" / "icon-128.png"), "assets"),
    (str(PROJECT_ROOT / "assets" / "icon.ico"), "assets"),
    (str(PROJECT_ROOT / "README.md"), "."),
]

# GUI frontend file-by-file so local donate QR images never ship.
_WEB_ROOT = PROJECT_ROOT / "aiharness" / "gui" / "web"
_DONATE_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
if _WEB_ROOT.is_dir():
    for _path in _WEB_ROOT.rglob("*"):
        if not _path.is_file():
            continue
        _rel = _path.relative_to(_WEB_ROOT)
        if (
            len(_rel.parts) >= 2
            and _rel.parts[0] == "donate"
            and _path.suffix.lower() in _DONATE_IMAGE_SUFFIXES
        ):
            continue
        datas.append(
            (
                str(_path),
                str(Path("aiharness/gui/web") / _rel.parent).replace("\\", "/"),
            )
        )
# Textual ships its own CSS and widget assets.
datas += collect_data_files("textual")

hiddenimports = [
    # Imported lazily inside functions to keep startup fast, so the static
    # analyser never sees them.
    "aiharness.tools.agents",
    "aiharness.tools.browser",
    "aiharness.tools.computer",
    "aiharness.tools.interaction",
    "aiharness.tools.market",
    "aiharness.tools.orchestrate",
    "aiharness.tools.team",
    "aiharness.tools.workflows",
    "aiharness.market.paper",
    "aiharness.market.router",
    # The tray is imported inside launch(), so nothing static references it.
    # Without these the packaged app silently has no tray icon and the close
    # button quits — which is precisely the behaviour the tray exists to stop.
    "aiharness.gui.tray",
    "aiharness.gui.capture",
    "aiharness.gui.region_select",
    "aiharness.gui.hotkey",
    "aiharness.gui.screenshot_service",
    "pystray",
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageGrab",
    "PIL.ImageTk",
    # Region-select overlay (must not be in excludes — see below).
    "tkinter",
    "tkinter.ttk",
    "_tkinter",
    "aiharness.gui.bridge",
    "aiharness.gui.commands",
    "aiharness.gui.desktop",
    "aiharness.gui.server",
    "aiharness.workflows.learning",
    "aiharness.workflows.orchestrator",
]
hiddenimports += collect_submodules("textual.widgets")

# Heavy optional dependencies. The harness runs without them and reports a
# clear install hint when they are missing, so bundling them would triple the
# download for features most users never enable.
# NOTE: do not exclude tkinter — interactive screenshot selection needs it,
# and PyInstaller must also pull in the Tcl/Tk runtime alongside _tkinter.
excludes = [
    "pyautogui", "playwright", "akshare", "pandas", "numpy",
    "matplotlib", "scipy", "IPython", "PyQt5", "PySide6",
]

analysis = Analysis(
    [str(PROJECT_ROOT / "aiharness" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

# Two executables from one analysis.
#
#   Dr.Wang.exe  windowed — opens the desktop UI with no console
#                       behind it. A GUI app that flashes a black terminal
#                       looks broken, and on Windows that console is a real
#                       second window the user has to close.
#   aih.exe             console — the CLI subcommands need somewhere to
#                       print, and `aih tui` needs an actual terminal.
COMMON = dict(
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX trips Windows Defender heuristics
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "assets" / "icon.ico"),
)

windowed = EXE(pyz, analysis.scripts, [], name=APP_NAME, console=False, **COMMON)
console = EXE(pyz, analysis.scripts, [], name="aih", console=True, **COMMON)

COLLECT(
    windowed,
    console,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
