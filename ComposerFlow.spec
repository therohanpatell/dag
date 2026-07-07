# PyInstaller spec — build with:  pyinstaller ComposerFlow.spec
# Produces dist/ComposerFlow.exe: a single-file Windows app that starts a local
# web server (Python standard library only) and opens the browser. No Streamlit,
# no third-party web framework — small and fast.
from PyInstaller.utils.hooks import collect_submodules

# Bundle the entire static frontend (HTML/CSS/JS + vendored Drawflow).
datas = [
    ("composer_flow/webapp/static", "composer_flow/webapp/static"),
]
hiddenimports = collect_submodules("composer_flow")

a = Analysis(
    ["run_app.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=["tkinter", "PySide6", "PyQt6", "PyQt5", "streamlit", "pandas",
              "numpy", "altair", "pyarrow", "matplotlib"],
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
    upx=False,
    console=True,   # shows the server URL; close the window to quit the app
    onefile=True,
    icon=None,
)
