"""
Local web-app version of the Meet transcriber (deployable).

Run:
    python app.py
A browser opens at http://127.0.0.1:5000 automatically.

Everything (audio capture + Whisper) runs locally on your laptop. On Stop, two
files are written to your Desktop:
    <name>.txt  -> clean plain text, no timestamps
    <name>.srt  -> subtitles with timecodes (elapsed from start of recording)
"""

import atexit
import datetime as dt
import json
import os
import queue
import shutil
import sys
import tempfile
import re
import threading
import time
import urllib.request
import uuid
import wave
import webbrowser

# In windowed (no-console) builds, stdout/stderr are None — give them a sink so
# print() and Flask/werkzeug logging never crash the app.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import numpy as np
import webrtcvad
from flask import Flask, Response, jsonify, request, send_file

from transcribe import (
    AudioCapture, TARGET_RATE, FRAME_MS, FRAME_SAMPLES, MAX_PHRASE_S,
    MIN_PHRASE_MS, SILENCE_TAIL_MS, PREROLL_MS, normalize_gain,
    list_loopback_devices, list_input_devices,
)

try:
    import noisereduce as nr
    _HAVE_NR = True
except Exception:
    _HAVE_NR = False

OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = os.environ.get("MEET_OLLAMA_MODEL", "llama3.2")

# Built-in local LLM (bundled) so AI cleanup works for everyone with no setup.
_LLM = None
_LLM_LOCK = threading.Lock()
try:
    import llama_cpp  # noqa: F401
    _HAVE_LLAMA = True
except Exception:
    _HAVE_LLAMA = False

# Phrases Whisper notoriously emits on silence/noise — drop if a whole line.
_HALLUCINATIONS = {
    "you", "thank you", "thanks", "thanks for watching",
    "thank you for watching", "thank you very much", "bye", "okay", "ok",
    "the", "yeah", "so", "uh", "um", "subscribe",
}

# When frozen by PyInstaller, data files live under sys._MEIPASS.
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if getattr(
    sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))


def resource_path(rel):
    return os.path.join(BASE_DIR, rel)


def model_location(name):
    """Use a bundled offline model folder if present, else the model name
    (which makes faster-whisper download it on first use)."""
    local = resource_path(os.path.join("models", f"faster-whisper-{name}"))
    return local if os.path.isdir(local) else name


def _real_documents_dir():
    """Resolve the user's actual Documents folder, respecting Windows folder
    redirection (e.g. Documents moved to another drive) -- os.path.expanduser
    alone ignores that and can silently write to a path Explorer doesn't
    actually show as "Documents" (this user's really lives on D:\\)."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            value, kind = winreg.QueryValueEx(key, "Personal")
            if kind == winreg.REG_EXPAND_SZ:
                value = winreg.ExpandEnvironmentStrings(value)
            if value and os.path.isdir(value):
                return value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def output_dir():
    """Save transcripts to the user's real Documents folder (not the OneDrive-
    synced one, and not just C:\\Users\\<you>\\Documents -- that can differ from
    the real, possibly-redirected Documents folder). A 'Meet Transcriber'
    subfolder is created there, consistently, every time."""
    d = os.path.join(_real_documents_dir(), "Meet Transcriber")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        fallback = os.path.join(os.path.expanduser("~"), "Meet Transcriber")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "MeetTranscriber")
    os.makedirs(d, exist_ok=True)
    return d


SETTINGS_PATH = os.path.join(_config_dir(), "settings.json")
DEFAULT_SETTINGS = {
    "save_audio": True,        # keep a .wav of live recordings, next to the transcript
    "auto_ai_clean": False,    # run local-AI cleanup automatically after each session
    "hotkey": "<ctrl>+<alt>+r",  # global hotkey to start/stop (or hold for) a live recording
    "cancel_hotkey": "<esc>",  # discard the in-progress recording without saving
    "push_to_talk": False,     # hold the hotkey to record, release to stop (vs. toggle)
    "theme": "dark",
    "default_model": "small",
    "default_language": "en",
    "history_limit": 20,       # how many past sessions to keep in the History list
    "auto_save_txt": True,     # write the .txt export to Documents automatically after every recording
}
_settings_lock = threading.Lock()

# Common OS/browser shortcuts that must never be a GLOBAL hotkey: the listener
# fires regardless of which window has focus, so e.g. picking Ctrl+R (browser
# refresh) will silently stop a recording the moment the user reflexively
# refreshes their meeting tab. This is what actually happened once already —
# see the 74s/2-line truncated session from 2026-07-28.
_MODIFIER_NAMES = {"ctrl", "alt", "shift", "cmd"}
RISKY_HOTKEYS = {
    "ctrl+r": "Refresh page (browser)",
    "f5": "Refresh page (browser)",
    "ctrl+f5": "Hard refresh (browser)",
    "ctrl+w": "Close tab",
    "ctrl+t": "New tab",
    "ctrl+shift+t": "Reopen closed tab",
    "ctrl+n": "New window",
    "ctrl+shift+n": "New incognito window",
    "alt+f4": "Close window",
    "ctrl+f4": "Close tab",
    "ctrl+d": "Bookmark page",
    "ctrl+l": "Focus address bar",
    "alt+d": "Focus address bar",
    "ctrl+p": "Print",
    "ctrl+s": "Save page",
    "ctrl+f": "Find on page",
    "alt+space": "Open window system menu",
}


def _hotkey_parts(combo):
    return [p for p in (combo or "").lower().replace(" ", "")
            .replace("<", "").replace(">", "").split("+") if p]


def hotkey_risk(combo):
    """None if combo looks safe as a GLOBAL hotkey, else a short reason it isn't."""
    parts = _hotkey_parts(combo)
    if not parts:
        return None
    if not any(p in _MODIFIER_NAMES for p in parts):
        return "No Ctrl/Alt/Shift — this would trigger during ordinary typing."
    key_set = frozenset(parts)
    for risky, label in RISKY_HOTKEYS.items():
        if frozenset(_hotkey_parts(risky)) == key_set:
            return f"Collides with a common shortcut: {label}."
    return None


def load_settings():
    with _settings_lock:
        data = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass
        return data


def save_settings(patch):
    with _settings_lock:
        data = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass
        data.update(patch)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return data


def srt_timecode(seconds):
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def mmss(seconds):
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def safe_stem(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in stem)
    return (keep.strip() or "file")[:40]


# Temp area for uploaded media. Per-process so two running instances can't wipe
# each other's folder; cleared on startup AND on exit so nothing the user
# transcribes is ever left behind — core to the privacy promise.
UPLOAD_DIR = os.path.join(tempfile.gettempdir(),
                          f"meet_transcriber_uploads_{os.getpid()}")


def _reset_upload_dir():
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)


_reset_upload_dir()
atexit.register(lambda: shutil.rmtree(UPLOAD_DIR, ignore_errors=True))


# Cache loaded Whisper models so repeated file uploads don't reload from disk.
_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()


def get_model(name):
    from faster_whisper import WhisperModel
    with _MODEL_LOCK:
        if name not in _MODEL_CACHE:
            _MODEL_CACHE[name] = WhisperModel(
                model_location(name), device="cpu", compute_type="int8")
        return _MODEL_CACHE[name]


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None   # allow large media uploads


# ------------------------------------------------------ session cache -----
# Sessions are cached locally (transcript + optional audio) regardless of
# whether the user ever exports anything. Documents\Meet Transcriber is only
# ever written to by an explicit Save action (see /session/<id>/export/<kind>)
# -- nothing is auto-written there on stop anymore.
_AUDIO_RETENTION = 8   # how many recent sessions keep their cached .wav around


def _sessions_root():
    d = os.path.join(_config_dir(), "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def session_cache_dir(session_id):
    d = os.path.join(_sessions_root(), session_id)
    os.makedirs(d, exist_ok=True)
    return d


def _index_path():
    return os.path.join(_sessions_root(), "index.json")


def _load_index():
    try:
        with open(_index_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_index(entries):
    with open(_index_path(), "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


_SESSION_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}_[0-9a-f]{6}$")


def _valid_session_id(session_id):
    """Session ids are always our own generated `<stamp>_<hex6>` -- reject
    anything else before it ever touches a filesystem path (these come
    straight from the URL, so this also rules out path traversal)."""
    return bool(_SESSION_ID_RE.match(session_id or ""))


def _load_transcript(session_id):
    if not _valid_session_id(session_id):
        raise ValueError("invalid session id")
    path = os.path.join(_sessions_root(), session_id, "transcript.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_transcript(session_id, transcript):
    path = os.path.join(session_cache_dir(session_id), "transcript.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(transcript, f, indent=2)


def save_session_cache(session_id, lines, kind, file_prefix, model_name, language,
                       translate, source_name, audio_path, clean_text, clean_engine):
    """Persist one finished session to the local cache and update the index.
    Called once per session, from _finalize() -- this (not Documents\\Meet
    Transcriber) is now the source of truth for the History list."""
    has_audio = bool(audio_path and os.path.exists(audio_path))
    transcript = {
        "session_id": session_id,
        "kind": kind,
        "file_prefix": file_prefix,
        "model": model_name,
        "language": language,
        "translate": bool(translate),
        "source_name": source_name,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(lines[-1]["end"], 1) if lines and "end" in lines[-1] else None,
        "lines": lines,
        "has_audio": has_audio,
        "clean_text": clean_text,
        "clean_engine": clean_engine,
    }
    _write_transcript(session_id, transcript)

    entries = _load_index()
    entry = {
        "session_id": session_id,
        "kind": kind,
        "created_at": transcript["created_at"],
        "duration_s": transcript["duration_s"],
        "count": len(lines),
        "source_name": source_name,
        "has_audio": has_audio,
        "has_clean": bool(clean_text),
        "exported": {},   # {"txt": "<saved filename>", ...} once each kind is exported
    }
    entries.insert(0, entry)

    settings = load_settings()
    limit = max(1, int(settings.get("history_limit") or DEFAULT_SETTINGS["history_limit"]))
    entries = entries[:limit]

    # Cap how many entries still keep a cached .wav -- audio is the disk-heavy
    # artifact (~2MB/min); older entries keep their transcript, lose the audio.
    kept_audio = 0
    for e in entries:
        if e.get("has_audio"):
            kept_audio += 1
            if kept_audio > _AUDIO_RETENTION:
                stale = os.path.join(_sessions_root(), e["session_id"], "audio.wav")
                if os.path.exists(stale):
                    try:
                        os.remove(stale)
                    except Exception:
                        pass
                e["has_audio"] = False

    # Drop cache folders for sessions that fell off the end of the index.
    kept_ids = {e["session_id"] for e in entries}
    try:
        for name in os.listdir(_sessions_root()):
            full = os.path.join(_sessions_root(), name)
            if os.path.isdir(full) and name not in kept_ids:
                shutil.rmtree(full, ignore_errors=True)
    except Exception:
        pass

    _save_index(entries)
    return entry


def _notify_tray(msg):
    """Best-effort tray balloon so a mid-recording warning (e.g. the other
    person's audio has gone silent) reaches the user even with the main
    window hidden -- not just the in-window notice banner."""
    try:
        import desktop
        if desktop.state.tray_icon is not None:
            desktop.state.tray_icon.notify(msg, "Local Meet Transcriber")
    except Exception:
        pass


class SessionMixin:
    """Shared event fan-out + transcript finalisation for live & file modes."""

    out_prefix = "meet_transcript"
    kind = "live"

    def _init_shared(self):
        self.lines = []                 # [{idx,start,end,text,wall}]
        self.subscribers = []
        self._stop = threading.Event()
        self.status = "starting"
        self.audio_path = None
        self._idx = 0
        # Computed once so live-streamed audio and the cached transcript share
        # the same id.
        self.stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_id = f"{self.stamp}_{uuid.uuid4().hex[:6]}"

    # ---- event fan-out -------------------------------------------------
    def _emit(self, payload):
        for q in list(self.subscribers):
            q.put(payload)

    def add_subscriber(self):
        q = queue.Queue()
        q.put({"type": "status", "status": self.status})
        for ln in self.lines:
            q.put({"type": "line", **ln})
        self.subscribers.append(q)
        return q

    def remove_subscriber(self, q):
        if q in self.subscribers:
            self.subscribers.remove(q)

    def _set_status(self, s, extra=None):
        self.status = s
        payload = {"type": "status", "status": s}
        if extra:
            payload.update(extra)
        self._emit(payload)

    def _discard_audio(self):
        if self.audio_path and os.path.exists(self.audio_path):
            try:
                os.remove(self.audio_path)
            except Exception:
                pass
            self.audio_path = None
        # The cache folder is created eagerly (to hold the incrementally-written
        # .wav) as soon as a live session starts -- clean it up too, or an empty
        # dir is left behind under sessions\ for every no-speech/cancelled take.
        session_id = getattr(self, "session_id", None)
        if session_id:
            d = os.path.join(_sessions_root(), session_id)
            if os.path.isdir(d) and not os.listdir(d):
                try:
                    os.rmdir(d)
                except Exception:
                    pass

    def _finalize(self):
        """Cache the transcript (+ audio, if captured) locally. Nothing is
        written to Documents\\Meet Transcriber here -- that only happens when
        the user explicitly exports a session (see /session/<id>/export/<kind>)."""
        time.sleep(0.6)   # let any in-flight segment drain
        if getattr(self, "_discard", False):
            # Cancelled (e.g. the cancel hotkey) — throw away everything, save nothing.
            self._discard_audio()
            self._set_status("stopped", {"count": 0, "cancelled": True})
            return
        if not self.lines:
            # Nothing was transcribed — don't cache a near-silent recording either.
            self._discard_audio()
            self._set_status("stopped", {"count": 0, "empty": True})
            return

        clean_text, clean_engine = None, None
        if getattr(self, "auto_clean", False):
            try:
                clean_text, clean_engine = clean_transcript(self.lines)
            except Exception:
                clean_text = None

        entry = save_session_cache(
            session_id=self.session_id,
            lines=self.lines,
            kind=self.kind,
            file_prefix=self.out_prefix,
            model_name=getattr(self, "model_name", None),
            language=getattr(self, "language", None),
            translate=getattr(self, "task", "transcribe") == "translate",
            source_name=getattr(self, "orig_name", None),
            audio_path=self.audio_path,
            clean_text=clean_text,
            clean_engine=clean_engine,
        )

        # Auto-save the .txt export so every finished recording leaves a real
        # file in Documents without requiring a manual click -- the one kind
        # that's always on by default; srt/audio/clean stay explicit-only.
        auto_saved_txt = False
        if load_settings().get("auto_save_txt", True):
            try:
                _export_session_file(self.session_id, "txt", t=None, entries=None)
                auto_saved_txt = True
            except Exception:
                pass

        self._set_status("stopped", {
            "session_id": self.session_id,
            "count": len(self.lines),
            "has_audio": bool(entry.get("has_audio")),
            "clean": bool(clean_text),
            "clean_engine": clean_engine,
            "auto_saved_txt": auto_saved_txt,
            "folder": output_dir() if auto_saved_txt else None,
        })

    def stop(self, discard=False):
        if discard:
            self._discard = True
        self._stop.set()


class Session(SessionMixin):
    """One live transcription run: capture -> VAD -> Whisper -> subscribers."""

    def __init__(self, model_name, language, use_mic, denoise=True,
                 loopback_index=None, mic_index=None, translate=False):
        self._init_shared()
        settings = load_settings()
        self.model_name = model_name
        self.language = language or None
        self.use_mic = use_mic
        self.denoise = denoise and _HAVE_NR
        self.loopback_index = loopback_index
        self.mic_index = mic_index
        self.task = "translate" if translate else "transcribe"
        self.save_audio = bool(settings.get("save_audio", True))
        self.auto_clean = bool(settings.get("auto_ai_clean", False))
        self._wav = None
        self.cap = None
        self.rec_start = None           # monotonic clock at first "listening"
        threading.Thread(target=self._run, daemon=True).start()

    # ---- main pipeline -------------------------------------------------
    def _run(self):
        try:
            self._set_status("loading model")
            model = get_model(self.model_name)

            self._set_status("opening audio")
            self.cap = AudioCapture(use_mic=self.use_mic,
                                    loopback_index=self.loopback_index,
                                    mic_index=self.mic_index)
            self.cap.start()

            seg_q = queue.Queue()
            threading.Thread(target=self._transcribe_worker,
                             args=(model, seg_q), daemon=True).start()

            if self.save_audio:
                self.audio_path = os.path.join(
                    session_cache_dir(self.session_id), "audio.wav")
                self._wav = wave.open(self.audio_path, "wb")
                self._wav.setnchannels(1)
                self._wav.setsampwidth(2)   # 16-bit PCM
                self._wav.setframerate(TARGET_RATE)

            self.rec_start = time.monotonic()
            self._set_status("listening")

            vad = webrtcvad.Vad(1)          # gentler: keeps quiet/short words
            preroll_n = max(1, PREROLL_MS // FRAME_MS)
            preroll = []                     # ring buffer of recent frames
            collected = []
            speech_started = False
            trailing_silence = 0
            phrase_start = 0.0
            last_speech = 0.0
            peak = 0.0                       # loudest sample seen (no-audio check)
            warned = False
            sys_audio_warned = False
            mic_warned = False
            ONE_SIDED_DEAD_S = 8.0            # sustained, not a brief stall

            while not self._stop.is_set():
                try:
                    frame = self.cap.out_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                now = time.monotonic() - self.rec_start

                # If we've been listening a while and the captured signal is
                # essentially silent, warn the user (common Bluetooth-headset
                # case: the device switches mode and loopback goes silent).
                peak = max(peak, float(np.max(np.abs(frame))) if len(frame) else 0.0)
                if not warned and now > 9 and peak < 0.012 and not self.lines:
                    self._emit({"type": "notice", "msg": "No audio detected on "
                                "this source. Try a different audio source "
                                "below, or check that the meeting plays through "
                                "it (Bluetooth headsets often go silent in calls)."})
                    warned = True

                # The check above only catches *total* silence. With a mic in
                # the mix, the overall signal can look perfectly healthy (your
                # own voice) while the system-audio side has been dead the
                # whole time -- the mixer falls back to mic-only so the
                # recording never blocks, but that fallback is otherwise
                # silent about it. Watch each source's own last-delivery time
                # so a one-sided recording gets flagged while it's still
                # happening, not discovered after the fact in the transcript.
                if self.use_mic and self.cap is not None:
                    loop_age = time.time() - getattr(self.cap, "last_loop_ts", time.time())
                    mic_age = time.time() - getattr(self.cap, "last_mic_ts", time.time())
                    if not sys_audio_warned and now > ONE_SIDED_DEAD_S + 1 and loop_age > ONE_SIDED_DEAD_S:
                        msg = ("System audio hasn't been picked up for a while -- "
                               "only your microphone is being recorded. The other "
                               "person's voice may be missing from this transcript. "
                               "Check your audio output device (Bluetooth headsets "
                               "often go silent mid-call) or pick a different "
                               "source below.")
                        self._emit({"type": "notice", "msg": msg})
                        _notify_tray(msg)
                        sys_audio_warned = True
                    if not mic_warned and now > ONE_SIDED_DEAD_S + 1 and mic_age > ONE_SIDED_DEAD_S:
                        msg = ("Your microphone hasn't been picked up for a while -- "
                               "only system audio is being recorded. Your own voice "
                               "may be missing from this transcript. Check that the "
                               "right microphone is selected below.")
                        self._emit({"type": "notice", "msg": msg})
                        _notify_tray(msg)
                        mic_warned = True
                pcm16 = (frame * 32767).astype(np.int16).tobytes()
                if self._wav is not None:
                    self._wav.writeframes(pcm16)   # record the whole session, not just speech
                is_speech = vad.is_speech(pcm16, TARGET_RATE)

                if is_speech:
                    if not speech_started:
                        # Prepend recent pre-roll so the first word isn't clipped.
                        collected = list(preroll)
                        phrase_start = max(0.0, now - len(preroll) * FRAME_MS / 1000.0)
                    collected.append(frame)
                    speech_started = True
                    trailing_silence = 0
                    last_speech = now
                elif speech_started:
                    collected.append(frame)
                    trailing_silence += FRAME_MS
                else:
                    preroll.append(frame)
                    if len(preroll) > preroll_n:
                        preroll.pop(0)

                dur_s = len(collected) * FRAME_MS / 1000.0
                if speech_started and (trailing_silence >= SILENCE_TAIL_MS
                                       or dur_s >= MAX_PHRASE_S):
                    if dur_s * 1000 >= MIN_PHRASE_MS:
                        seg_q.put((np.concatenate(collected),
                                   phrase_start, last_speech))
                    collected = []
                    speech_started = False
                    trailing_silence = 0
                    preroll = []
        except Exception as e:
            self._set_status(f"error: {e}")
            return
        finally:
            if self.cap:
                self.cap.stop()
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:
                    pass

        self._finalize()

    def _transcribe_worker(self, model, seg_q):
        prev_text = ""
        while not self._stop.is_set() or not seg_q.empty():
            try:
                audio, start, end = seg_q.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue

            # Energy gate: skip near-silent segments so Whisper can't
            # hallucinate filler ("You", "Thank you") between real speech.
            rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
            if rms < 0.004:
                continue

            # Clean up the audio before recognition: optional noise reduction,
            # then auto-gain so faint remote speakers are loud enough to hear.
            if self.denoise:
                try:
                    audio = nr.reduce_noise(y=audio, sr=TARGET_RATE,
                                            stationary=False).astype(np.float32)
                except Exception:
                    pass
            audio = normalize_gain(audio)

            # Feed the tail of the previous line as context (better accuracy,
            # fewer fragmented mis-hears) without full conditioning loops.
            prompt = prev_text[-200:] if prev_text else None
            segments, _ = model.transcribe(
                audio, language=self.language, task=self.task, beam_size=5,
                vad_filter=False, condition_on_previous_text=False,
                initial_prompt=prompt, no_speech_threshold=0.6,
                compression_ratio_threshold=2.4, log_prob_threshold=-1.0)
            # Keep only confident segments (drops low-quality hallucinations).
            kept = [s.text.strip() for s in segments
                    if getattr(s, "no_speech_prob", 0.0) < 0.6
                    and getattr(s, "avg_logprob", 0.0) > -1.1]
            text = " ".join(kept).strip()
            # Drop lone filler phrases that Whisper emits on silence/noise.
            if text.lower().strip(" .!,") in _HALLUCINATIONS:
                text = ""
            if text:
                prev_text = (prev_text + " " + text).strip()
                self._idx += 1
                wall = dt.datetime.now().strftime("%H:%M:%S")
                line = {"idx": self._idx, "start": round(start, 3),
                        "end": round(end, 3), "text": text, "wall": wall}
                self.lines.append(line)
                self._emit({"type": "line", **line})

class FileSession(SessionMixin):
    """Transcribe an uploaded media file locally, then delete the upload."""

    kind = "file"

    def __init__(self, file_path, orig_name, model_name, language, denoise=True,
                 translate=False):
        self._init_shared()
        self.file_path = file_path
        self.orig_name = orig_name
        self.out_prefix = "transcript_" + safe_stem(orig_name)
        self.model_name = model_name
        self.language = language or None
        self.denoise = denoise and _HAVE_NR
        self.task = "translate" if translate else "transcribe"
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self._set_status("loading model")
            model = get_model(self.model_name)

            self._set_status("transcribing")
            source = self.file_path
            if self.denoise:
                # Decode to 16 kHz mono, denoise, then transcribe the array.
                from faster_whisper.audio import decode_audio
                audio = decode_audio(self.file_path, sampling_rate=TARGET_RATE)
                try:
                    audio = nr.reduce_noise(y=audio, sr=TARGET_RATE,
                                            stationary=False).astype(np.float32)
                except Exception:
                    pass
                source = normalize_gain(audio)

            segments, _ = model.transcribe(
                source, language=self.language, task=self.task, beam_size=5,
                vad_filter=True, condition_on_previous_text=True,
                no_speech_threshold=0.6)

            for s in segments:
                if self._stop.is_set():
                    break
                text = s.text.strip()
                if not text:
                    continue
                self._idx += 1
                line = {"idx": self._idx, "start": round(s.start, 3),
                        "end": round(s.end, 3), "text": text,
                        "wall": mmss(s.start)}
                self.lines.append(line)
                self._emit({"type": "line", **line})
        except Exception as e:
            self._set_status(f"error: {e}")
            return
        finally:
            # Privacy: remove the uploaded media as soon as we're done with it.
            try:
                os.remove(self.file_path)
            except Exception:
                pass

        self._finalize()


session = None
lock = threading.Lock()


@app.route("/")
def index():
    return send_file(resource_path(os.path.join("templates", "index.html")))


@app.route("/devices")
def devices():
    """List system-audio (loopback) sources + microphones so the user can pick."""
    try:
        loop = [{"index": i, "name": n} for i, n in list_loopback_devices()]
        mics = [{"index": i, "name": n} for i, n in list_input_devices()]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "devices": loop, "loopback": loop, "inputs": mics})


def start_live_session(model="small", language=None, mic=True, denoise=True,
                       device=None, micDevice=None, translate=False):
    """Shared start logic used by the /start route, the tray menu, and the
    global hotkey. Returns (ok: bool, error: str|None)."""
    global session
    with lock:
        if session and not session._stop.is_set() and session.status != "stopped":
            return False, "already running"
        session = Session(
            model_name=model,
            language=language or None,
            use_mic=bool(mic),
            denoise=bool(denoise),
            loopback_index=int(device) if device not in (None, "", "auto") else None,
            mic_index=int(micDevice) if micDevice not in (None, "", "auto") else None,
            translate=bool(translate),
        )
    return True, None


def stop_current_session():
    """Shared stop logic used by the /stop route, the tray menu, and the
    global hotkey. Returns (ok: bool, error: str|None)."""
    if session:
        session.stop()
        return True, None
    return False, "no session"


def cancel_current_session():
    """Discard the in-progress recording (transcript + audio) without saving
    anything — used by the cancel hotkey (default Escape)."""
    if session and not session._stop.is_set():
        session.stop(discard=True)
        return True, None
    return False, "no active session"


def current_status():
    """('idle'|'listening'|'transcribing'|'busy'|<other>, is_active: bool)."""
    if not session:
        return "idle", False
    s = session.status
    active = s in ("listening", "transcribing", "opening audio", "loading model")
    return s, active


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(force=True) or {}
    ok, err = start_live_session(
        model=data.get("model", "small"), language=data.get("language"),
        mic=data.get("mic", True), denoise=data.get("denoise", True),
        device=data.get("device"), micDevice=data.get("micDevice"),
        translate=data.get("translate", False))
    if not ok:
        return jsonify({"ok": False, "error": err}), 409
    return jsonify({"ok": True})


@app.route("/status")
def status():
    """Lightweight polling endpoint for the tray icon and the main window --
    includes elapsed seconds so a window opened after recording already
    started (e.g. via the global hotkey) can show an accurate timer."""
    s, active = current_status()
    elapsed = None
    if session is not None and getattr(session, "rec_start", None) is not None:
        elapsed = time.monotonic() - session.rec_start
    return jsonify({"ok": True, "status": s, "active": active,
                    "count": len(session.lines) if session else 0,
                    "elapsed": elapsed})


@app.route("/history")
def history():
    """List cached sessions (most recent first) for the sidebar. Backed by the
    session cache, not a scan of Documents\\Meet Transcriber -- nothing is
    written there until the user explicitly exports a session."""
    return jsonify({"ok": True, "sessions": _load_index()})


@app.route("/session/<session_id>")
def session_detail(session_id):
    """Full transcript + metadata for the History detail view."""
    try:
        t = _load_transcript(session_id)
    except Exception:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "session": t})


@app.route("/session/<session_id>", methods=["DELETE"])
def session_delete(session_id):
    """Remove a session from the cache/History list only -- any copies already
    exported to Documents\\Meet Transcriber are left untouched."""
    if not _valid_session_id(session_id):
        return jsonify({"ok": False, "error": "invalid id"}), 400
    entries = [e for e in _load_index() if e["session_id"] != session_id]
    _save_index(entries)
    shutil.rmtree(os.path.join(_sessions_root(), session_id), ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/session/<session_id>/audio")
def session_audio(session_id):
    """Stream cached audio inline for in-page playback (not a save action)."""
    if not _valid_session_id(session_id):
        return "Not found", 404
    path = os.path.join(_sessions_root(), session_id, "audio.wav")
    if not os.path.isfile(path):
        return "No audio for this session", 404
    return send_file(path)


def _export_session_file(session_id, kind, t=None, entries=None):
    """Write exactly one export kind (txt|srt|audio|clean) for a session into
    Documents\\Meet Transcriber, update the index's `exported` record, and
    return the file path. Shared by the /export route and the auto-save-txt
    step in _finalize() -- one place that actually writes these files.

    Raises FileNotFoundError if audio was requested but none was captured,
    RuntimeError (propagated from clean_transcript) if "clean" has no local
    AI engine available, or any other Exception on a genuine write failure."""
    if t is None:
        t = _load_transcript(session_id)
    if entries is None:
        entries = _load_index()
    entry = next((e for e in entries if e["session_id"] == session_id), None)

    # Re-serve a prior export of this exact kind instead of re-writing it.
    existing = (entry or {}).get("exported", {}).get(kind)
    if existing:
        path = os.path.join(output_dir(), existing)
        if os.path.isfile(path):
            return path

    prefix = t.get("file_prefix") or "meet_transcript"
    dpath = output_dir()

    if kind == "txt":
        fname = f"{prefix}_{session_id}.txt"
        path = os.path.join(dpath, fname)
        with open(path, "w", encoding="utf-8") as f:
            for ln in t["lines"]:
                f.write(ln["text"] + "\n")

    elif kind == "srt":
        fname = f"{prefix}_{session_id}.srt"
        path = os.path.join(dpath, fname)
        with open(path, "w", encoding="utf-8") as f:
            for ln in t["lines"]:
                f.write(f"{ln['idx']}\n")
                f.write(f"{srt_timecode(ln['start'])} --> "
                        f"{srt_timecode(ln['end'])}\n")
                f.write(ln["text"] + "\n\n")

    elif kind == "audio":
        src = os.path.join(_sessions_root(), session_id, "audio.wav")
        if not os.path.isfile(src):
            raise FileNotFoundError("no audio for this session")
        fname = f"{prefix}_{session_id}.wav"
        path = os.path.join(dpath, fname)
        shutil.copyfile(src, path)

    elif kind == "clean":
        clean_text, clean_engine = t.get("clean_text"), t.get("clean_engine")
        if not clean_text:
            clean_text, clean_engine = clean_transcript(t["lines"])  # may raise RuntimeError
            t["clean_text"], t["clean_engine"] = clean_text, clean_engine
            _write_transcript(session_id, t)
            if entry is not None:
                entry["has_clean"] = True
        fname = f"{prefix}_{session_id}_clean.txt"
        path = os.path.join(dpath, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(clean_text)

    else:
        raise ValueError(f"unknown export kind: {kind}")

    if entry is not None:
        entry.setdefault("exported", {})[kind] = os.path.basename(path)
        _save_index(entries)
    return path


@app.route("/session/<session_id>/export/<kind>")
def session_export(session_id, kind):
    """Save exactly one file (txt|srt|audio|clean) for this session into
    Documents\\Meet Transcriber -- the explicit, one-kind-at-a-time replacement
    for the old always-bundle-everything behavior."""
    if kind not in ("txt", "srt", "audio", "clean"):
        return "Unknown export kind", 400
    try:
        t = _load_transcript(session_id)
    except Exception:
        return "Not found", 404
    try:
        path = _export_session_file(session_id, kind, t=t)
    except FileNotFoundError:
        return "No audio for this session", 404
    except RuntimeError:
        return jsonify({"ok": False, "error": "ollama_not_running",
                        "help": "Install Ollama from ollama.com, then run: "
                                "ollama pull llama3.2"}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return send_file(path, as_attachment=True)


@app.route("/open-folder/<which>", methods=["POST"])
def open_folder(which):
    """Open a folder in Explorer -- fixed allowlist only, never an arbitrary
    path, since `which` comes straight from the page."""
    paths = {"appdata": _config_dir(), "documents": output_dir()}
    path = paths.get(which)
    if not path:
        return jsonify({"ok": False, "error": "unknown folder"}), 400
    try:
        os.startfile(path)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


def _settings_payload(data):
    return {"ok": True, "settings": data,
            "hotkey_warning": hotkey_risk(data.get("hotkey")),
            "cancel_hotkey_warning": hotkey_risk(data.get("cancel_hotkey"))}


@app.route("/settings", methods=["GET", "POST"])
def settings_route():
    if request.method == "POST":
        patch = request.get_json(force=True) or {}
        patch = {k: v for k, v in patch.items() if k in DEFAULT_SETTINGS}
        data = save_settings(patch)
        if {"hotkey", "cancel_hotkey", "push_to_talk"} & patch.keys():
            try:
                import desktop
                desktop.apply_hotkey(data["hotkey"], data["cancel_hotkey"],
                                     data["push_to_talk"])
            except Exception:
                pass
        return jsonify(_settings_payload(data))
    return jsonify(_settings_payload(load_settings()))


@app.route("/hotkey-risk")
def hotkey_risk_route():
    """Live collision check the Settings UI calls as the user types a combo."""
    return jsonify({"ok": True, "warning": hotkey_risk(request.args.get("combo", ""))})


@app.route("/autostart", methods=["GET", "POST"])
def autostart_route():
    """'Launch at Windows startup' toggle -- off unless the user explicitly
    turns it on from Settings (see autostart.py for the HKCU Run-key mechanism)."""
    import autostart
    if request.method == "POST":
        patch = request.get_json(force=True) or {}
        try:
            autostart.set_enabled(bool(patch.get("enabled")))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    try:
        enabled = autostart.is_enabled()
    except Exception:
        enabled = False
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/cancel", methods=["POST"])
def cancel_route():
    ok, err = cancel_current_session()
    if not ok:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True})


@app.route("/upload", methods=["POST"])
def upload():
    """Transcribe an uploaded audio/video file locally; the file is deleted
    immediately after transcription (and the temp dir is wiped on exit)."""
    global session
    with lock:
        if session and not session._stop.is_set() and session.status != "stopped":
            return jsonify({"ok": False, "error": "already running"}), 409
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"ok": False, "error": "no file"}), 400
        ext = os.path.splitext(f.filename)[1][:12]
        os.makedirs(UPLOAD_DIR, exist_ok=True)   # defensive: dir may have been wiped
        tmp = os.path.join(UPLOAD_DIR, uuid.uuid4().hex + ext)
        f.save(tmp)
        session = FileSession(
            file_path=tmp, orig_name=f.filename,
            model_name=request.form.get("model", "small"),
            language=request.form.get("language") or None,
            denoise=(request.form.get("denoise", "true") == "true"),
            translate=(request.form.get("translate", "false") == "true"),
        )
    return jsonify({"ok": True})


@app.route("/stop", methods=["POST"])
def stop():
    ok, err = stop_current_session()
    if not ok:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    if not session:
        return Response("data: {}\n\n", mimetype="text/event-stream")
    sess = session
    q = sess.add_subscriber()

    def gen():
        try:
            while True:
                try:
                    yield f"data: {json.dumps(q.get(timeout=15))}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            sess.remove_subscriber(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})



def ollama_available():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2) as r:
            tags = json.loads(r.read().decode("utf-8"))
        models = [m.get("name", "") for m in tags.get("models", [])]
        return True, models
    except Exception:
        return False, []


def _strip_preamble(s):
    """Remove the chatty preface/quotes some models add around their output."""
    s = s.strip()
    lines = s.split("\n")
    if lines and re.match(r"^\s*(here(\s+is|'s|’s)?|sure|okay|ok|the\s+cleaned"
                          r"|cleaned\s+transcript).{0,60}:\s*$", lines[0], re.I):
        s = "\n".join(lines[1:]).strip()
    if len(s) >= 2 and s[0] in '"“' and s[-1] in '"”':
        s = s[1:-1].strip()
    return s


def ollama_clean(text, model):
    prompt = (
        "Clean up this meeting transcript: fix punctuation, capitalization, and "
        "obvious speech-to-text errors, and remove filler words (um, uh, you know). "
        "Keep the original wording and meaning. Do NOT summarize, translate, add, or "
        "invent anything. Output ONLY the corrected transcript text — no preamble, "
        "no explanation, no quotation marks, no markdown.\n\nTRANSCRIPT:\n" + text)
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.1}}).encode("utf-8")
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(r.read().decode("utf-8")).get("response", "").strip()
    return _strip_preamble(out)


_CLEAN_SYS = (
    "You are a transcript editor. Fix punctuation, capitalization and obvious "
    "speech-to-text errors, and remove filler words (um, uh, you know). Keep the "
    "original wording, language and meaning. Do NOT summarize, translate, add or "
    "invent anything. Output ONLY the corrected transcript text — no preamble, no "
    "explanation, no quotation marks, no markdown.")


def llm_path():
    return resource_path(os.path.join("models", "llm",
                                      "Llama-3.2-1B-Instruct-Q4_K_M.gguf"))


def get_llm():
    """Lazy-load the bundled GGUF model (CPU)."""
    global _LLM
    with _LLM_LOCK:
        if _LLM is None:
            from llama_cpp import Llama
            _LLM = Llama(model_path=llm_path(), n_ctx=4096,
                         n_threads=os.cpu_count() or 4, verbose=False)
        return _LLM


def llama_clean(text):
    llm = get_llm()
    out = llm.create_chat_completion(
        messages=[{"role": "system", "content": _CLEAN_SYS},
                  {"role": "user", "content": text}],
        temperature=0.1, max_tokens=2048)
    return _strip_preamble(out["choices"][0]["message"]["content"].strip())


def _chunks(lines, max_chars=4500):
    """Group transcript lines into context-sized chunks for the small LLM."""
    buf, n, out = [], 0, []
    for ln in lines:
        t = ln["text"]
        if n + len(t) > max_chars and buf:
            out.append("\n".join(buf)); buf, n = [], 0
        buf.append(t); n += len(t) + 1
    if buf:
        out.append("\n".join(buf))
    return out


def clean_transcript(lines):
    """Clean the transcript with a local LLM via Ollama (CPU-portable, GPU-aware).
    Optionally uses a bundled llama.cpp model if one is present AND the CPU
    supports it (skipped by default — prebuilt llama.cpp needs AVX2)."""
    chunks = _chunks(lines)
    ok, models = ollama_available()
    if ok and models:
        model = OLLAMA_MODEL
        if model not in models and not any(m.startswith(model) for m in models):
            model = models[0]
        cleaned = "\n\n".join(ollama_clean(c, model) for c in chunks).strip()
        if cleaned:
            return cleaned, f"Ollama · {model}"
    if _HAVE_LLAMA and os.path.exists(llm_path()):
        try:
            cleaned = "\n\n".join(llama_clean(c) for c in chunks).strip()
            if cleaned:
                return cleaned, "Llama 3.2 (built-in)"
        except Exception:
            pass
    raise RuntimeError("no_ai_engine")




PORT = int(os.environ.get("MEET_PORT", "5000"))


def _serve():
    app.run(host="127.0.0.1", port=PORT, threaded=True,
            debug=False, use_reloader=False)


def _wait_until_up(timeout=20):
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", PORT), 0.4):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    # Self-test: verify bundled deps in the frozen exe, then exit. Results are
    # written to a file (not just printed) because windowed builds have no
    # stdout to read — see the devnull guard near the top of this file.
    if os.environ.get("MEET_SELFTEST"):
        lines = []
        try:
            import webview  # noqa
            lines.append("webview: ok")
        except Exception as e:
            lines.append(f"webview: FAIL {e}")
        for mod in ("pystray", "pynput", "desktop"):
            try:
                __import__(mod)
                lines.append(f"{mod}: ok")
            except Exception as e:
                lines.append(f"{mod}: FAIL {e}")
        lines.append(f"llama: have={_HAVE_LLAMA} model={os.path.exists(llm_path())}")
        for m in ("tiny", "base", "small"):
            lines.append(f"model {m}: {os.path.isdir(model_location(m))}")
        out_path = os.environ.get("MEET_SELFTEST_OUT") or os.path.join(
            tempfile.gettempdir(), "meet_transcriber_selftest.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        for line in lines:
            print("SELFTEST", line)
        sys.exit(0)

    threading.Thread(target=_serve, daemon=True).start()
    _wait_until_up()
    url = f"http://127.0.0.1:{PORT}"

    # Dev/fallback: MEET_BROWSER=1 opens a normal browser tab instead of the app window.
    if os.environ.get("MEET_BROWSER"):
        print(f"\n  Meet Transcriber  ->  {url}\n")
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    # Default: open as a standalone native desktop app window (no browser),
    # plus a system tray icon and a global hotkey (Ctrl+Alt+R) -- so the app
    # can run in the background like a proper desktop utility instead of
    # demanding the window stay open. Recording status/timer live in this
    # one window (it syncs itself to any already-running session on load),
    # not in a second floating overlay.
    # --start-hidden (set by autostart.py's registered launch command) skips
    # showing the main window at all -- tray + hotkey still start normally,
    # since desktop.start() below wires those up unconditionally either way.
    start_hidden = "--start-hidden" in sys.argv
    try:
        import webview
        main_win = webview.create_window(
            "Local Meet Transcriber", url,
            width=1180, height=820, min_size=(390, 600),
            background_color="#0B0F1A", hidden=start_hidden)

        def _on_started():
            try:
                import desktop
                desktop.start(main_win, start_hidden=start_hidden)
            except Exception as e:
                print("Desktop shell (tray/hotkey) unavailable:", e)

        webview.start(func=_on_started)   # blocks until the app really quits
    except Exception as e:
        print("Native window unavailable, opening browser instead:", e)
        webbrowser.open(url)
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    # NOTE: prefer running main.py instead of this file directly. desktop.py
    # does `import app`, which only reuses this same running module (with its
    # live `session` state) if THIS module was itself imported under the name
    # "app" — i.e. launched via main.py. Running `python app.py` directly
    # makes desktop.py re-import a second, disconnected copy of this module,
    # so the tray/hotkey would control a different session than the UI.
    main()
