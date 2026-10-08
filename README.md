# Local Meet Transcriber

A free, fully offline meeting transcriber for Windows. Captures system audio
(what you hear) and your microphone, transcribes in real time with a local
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) model, and saves
a plain-text transcript automatically — no account, no upload, no cloud API.

Built for therapists, coaches, and anyone who needs a private record of a
call: audio and transcripts never leave your machine.

## Features

- **One global hotkey** (`Ctrl+Alt+R` by default, configurable) starts/stops
  recording from anywhere — no need to keep the window open or focused.
- **Runs in the background**: lives in the system tray, can launch at
  Windows startup, and stays out of the way until you need it.
- **Auto-saves a `.txt` transcript** after every recording — no extra click.
  Export `.srt` or the raw audio separately, only when you want them.
- **100% local**: speech recognition runs on your own CPU via
  faster-whisper. Nothing is sent anywhere.
- **History**: past sessions are cached locally so you can revisit, clean
  up with a local AI pass, or export them later.

## Install

Download the latest `MeetTranscriber_Setup.exe` from
[Releases](../../releases/latest), run it, and launch
**Local Meet Transcriber** from the Start Menu.

Windows SmartScreen may warn that the app is from an "unknown publisher" —
that's expected for an independently published tool with no commercial code
signing certificate. Click **More info → Run anyway** if you trust the
source.

## Privacy & consent — please read

This app records **system audio and your microphone**, which means it can
capture everything said in a meeting or call, including the other
participant(s). Recording a conversation without the knowledge or consent
of everyone on the call is **illegal in many places** (for example, several
U.S. states require all-party consent). You are responsible for getting
consent before recording. This tool only runs what you point it at —
nothing is transcribed or saved without you starting a recording.

## Building from source

```bash
pip install -r requirements.txt
python build_tools/download_models.py   # fetches the bundled Whisper models
python main.py
```

To build the installer yourself:

```bash
python -m PyInstaller build_tools/MeetTranscriber.spec --noconfirm
iscc build_tools/installer.iss
```

## License

See [LICENSE](LICENSE). Bundled third-party components (faster-whisper,
PyTorch/ctranslate2, PyAudioWPatch, etc.) retain their own licenses.
