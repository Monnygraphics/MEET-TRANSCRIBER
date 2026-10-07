# PyInstaller spec for the Local Meet Transcriber (offline, model bundled).
# Build from the project root:
#     pyinstaller build_tools/MeetTranscriber.spec --noconfirm
import os
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

ROOT = os.path.abspath(os.getcwd())

datas, binaries, hiddenimports = [], [], []

HOOKS = os.path.join(ROOT, "build_tools", "hooks")

# Native + data heavy packages -> grab everything so the frozen app is self-contained.
# webview/pythonnet/clr_loader power the standalone native desktop window.
for pkg in ("ctranslate2", "faster_whisper", "av", "tokenizers",
            "onnxruntime", "pyaudiowpatch", "scipy", "soxr", "noisereduce",
            "webview", "pythonnet", "clr_loader", "pystray", "PIL", "pynput"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print(f"[spec] skip {pkg}: {e}")

# webrtcvad: bring its C extension; metadata handled by our override hook.
binaries += collect_dynamic_libs("webrtcvad")

# pywebview Windows (EdgeChromium/WebView2) backend bits.
hiddenimports += ["webview.platforms.winforms", "clr", "encodings.idna"]

# Tray icon (pystray) + global hotkey (pynput) backends on Windows.
hiddenimports += ["pystray._win32", "pynput.keyboard._win32", "pynput.mouse._win32"]

# App resources: UI templates + bundled offline models.
datas += [
    (os.path.join(ROOT, "templates"), "templates"),
    (os.path.join(ROOT, "models"), "models"),
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["transcribe", "webrtcvad", "_webrtcvad", "desktop", "autostart"],
    hookspath=[HOOKS],
    runtime_hooks=[],
    # Exclude torch & friends: noisereduce imports them only inside try/except
    # and falls back to the numpy/scipy path. Keeps the build ~2GB smaller.
    excludes=["tkinter", "matplotlib", "torch", "torchvision", "torchaudio",
              "numba", "llvmlite"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MeetTranscriber",
    console=False,           # windowed app — no CMD window pops up
    icon=os.path.join(ROOT, "icon.ico"),
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="MeetTranscriber",
)
