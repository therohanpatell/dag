# PyInstaller spec — build with:  pyinstaller ComposerFlow.spec
# Produces dist/ComposerFlow.exe (single file, windowed, no console).
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=collect_submodules("composer_flow"),
    excludes=[
        # Trim unused Qt modules to shrink the exe
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia", "PySide6.QtQml", "PySide6.QtQuick",
        "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest",
        "tkinter",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ComposerFlow",
    debug=False,
    strip=False,
    upx=False,           # UPX often triggers AV false positives — keep off
    console=False,       # windowed app; logs go to %LOCALAPPDATA%/ComposerFlow/logs
    onefile=True,
    icon=None,           # add "assets/app.ico" here when available
)
