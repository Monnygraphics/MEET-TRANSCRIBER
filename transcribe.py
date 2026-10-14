"""
Free, fully-local, real-time Google Meet (or any app) transcriber for Windows.

What it does
------------
- Captures your system audio via WASAPI *loopback* (everything you HEAR: all
  remote participants) and optionally MIXES IN your microphone (your own voice),
  so you get a complete meeting transcript.
- Detects speech vs silence (WebRTC VAD) and sends each spoken phrase to a local
  Whisper model (faster-whisper). Nothing is uploaded anywhere.
- Prints lines live in the terminal AND appends them to a timestamped .txt file.

Usage
-----
    python transcribe.py                 # mic + system audio, model "base"
    python transcribe.py --no-mic        # only what you hear (remote people)
    python transcribe.py --model small   # more accurate, a bit slower
    python transcribe.py --language en   # lock the language (faster, fewer errors)
    python transcribe.py --list-devices  # show audio devices and exit

Press Ctrl+C to stop. The transcript path is printed at startup.
"""

import argparse
import datetime as dt
import queue
import re
import sys
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio
import webrtcvad

try:
    import soxr  # high-quality resampler (no aliasing) -> cleaner audio for Whisper
    _HAVE_SOXR = True
except Exception:
    _HAVE_SOXR = False


def resample(audio, src_rate, dst_rate):
    """High-quality resample via soxr; numpy linear interp as a fallback."""
    if src_rate == dst_rate or len(audio) == 0:
        return audio.astype(np.float32)
    if _HAVE_SOXR:
        return soxr.resample(audio, src_rate, dst_rate, quality="HQ").astype(np.float32)
    n_out = int(round(len(audio) * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)

# Whisper expects 16 kHz mono audio.
TARGET_RATE = 16000
FRAME_MS = 30                      # WebRTC VAD wants 10/20/30 ms frames
FRAME_SAMPLES = TARGET_RATE * FRAME_MS // 1000   # 480 samples @ 16 kHz
SILENCE_TAIL_MS = 700              # end a phrase after this much trailing silence
MAX_PHRASE_S = 24                  # allow longer, more coherent chunks for context
MIN_PHRASE_MS = 120                # keep short words ("yes", "no") instead of dropping
PREROLL_MS = 200                   # audio kept before speech starts so first word isn't clipped


def normalize_gain(audio, target_peak=0.95, max_gain=8.0):
    """Auto-boost quiet audio (e.g. faint remote speakers) without clipping."""
    if len(audio) == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak < 1e-4:
        return audio
    gain = min(target_peak / peak, max_gain)
    return (audio * gain).astype(np.float32)


def list_devices():
    pa = pyaudio.PyAudio()
    print("\n--- Input devices (microphones) ---")
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d.get("maxInputChannels", 0) > 0:
            print(f"  [{i}] {d['name']}  ({int(d['defaultSampleRate'])} Hz)")
    print("\n--- WASAPI loopback devices (system audio) ---")
    for d in pa.get_loopback_device_info_generator():
        print(f"  [{d['index']}] {d['name']}  ({int(d['defaultSampleRate'])} Hz)")
    pa.terminate()


def to_mono_16k(buf_bytes, src_rate, src_channels):
    """Bytes (int16 interleaved) -> float32 mono @ 16 kHz in [-1, 1]."""
    audio = np.frombuffer(buf_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if src_channels > 1:
        audio = audio.reshape(-1, src_channels).mean(axis=1)
    if src_rate != TARGET_RATE:
        audio = resample(audio, src_rate, TARGET_RATE)
    return audio.astype(np.float32)


def list_loopback_devices():
    """[(index, name)] of WASAPI loopback (system-audio) devices, for the UI."""
    pa = pyaudio.PyAudio()
    try:
        return [(int(d["index"]), d["name"])
                for d in pa.get_loopback_device_info_generator()]
    finally:
        pa.terminate()


def list_input_devices_raw(pa):
    """[(index, name)] of real microphones, using an already-open PyAudio."""
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
    seen, out = set(), []
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if (d.get("maxInputChannels", 0) > 0
                and d.get("hostApi") == wasapi
                and not d.get("isLoopbackDevice", False)
                and "[Loopback]" not in d["name"]):
            key = d["name"]
            if key not in seen:
                seen.add(key)
                out.append((int(d["index"]), d["name"]))
    return out


def list_input_devices():
    """[(index, name)] of real microphones (WASAPI inputs, no loopbacks)."""
    pa = pyaudio.PyAudio()
    try:
        return list_input_devices_raw(pa)
    finally:
        pa.terminate()


def _device_core_name(name):
    """The part of a WASAPI device name that identifies the physical
    hardware, e.g. 'Headphones (RBT-SPEAKER) [Loopback]' -> 'rbt-speaker'."""
    m = re.search(r"\(([^)]+)\)", name)
    core = (m.group(1) if m else name).strip().lower()
    return re.sub(r"\s+hands-free.*$", "", core).strip()


def _same_physical_device(name_a, name_b):
    """True if two device names look like the same physical hardware (e.g. a
    Bluetooth headset's mic and its own output/loopback) -- the common case
    where picking that mic forces the device into a different Bluetooth
    profile and breaks the OTHER stream we're also capturing from it."""
    a, b = _device_core_name(name_a), _device_core_name(name_b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


class AudioCapture:
    """Opens loopback (+ optional mic), mixes to 16 kHz mono, yields chunks.

    Device selection re-checked every DEVICE_RECHECK_S while left on "auto"
    (no explicit index passed in), so connecting headphones or a Bluetooth
    device mid-call is picked up without restarting the recording -- see
    _follow_default_devices.
    """

    DEVICE_RECHECK_S = 2.0

    def __init__(self, use_mic=True, loopback_index=None, mic_index=None):
        self.use_mic = use_mic
        self.pa = pyaudio.PyAudio()
        self._loop_q = queue.Queue()
        self._mic_q = queue.Queue()
        self.out_q = queue.Queue()
        self._streams = []
        self._stop = threading.Event()
        self.last_loop_ts = self.last_mic_ts = time.time()
        self._auto_loop = loopback_index is None
        self._auto_mic = mic_index is None

        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        self.loop_dev = None
        if loopback_index is not None:
            # User explicitly chose the system-audio source.
            try:
                d = self.pa.get_device_info_by_index(int(loopback_index))
                if d.get("maxInputChannels", 0) > 0:
                    self.loop_dev = d
            except Exception:
                self.loop_dev = None
        if self.loop_dev is None:
            self.loop_dev = self._loopback_for_current_default(wasapi)
        if self.loop_dev is None:  # fallback: first loopback device
            self.loop_dev = next(self.pa.get_loopback_device_info_generator())
        print(f"  System audio : {self.loop_dev['name']}")

        self.mic_dev = None
        if use_mic:
            if mic_index is not None:
                try:
                    d = self.pa.get_device_info_by_index(int(mic_index))
                    if d.get("maxInputChannels", 0) > 0:
                        self.mic_dev = d
                except Exception:
                    self.mic_dev = None
            if self.mic_dev is None:
                self.mic_dev = self._best_auto_mic(wasapi)
            print(f"  Microphone   : {self.mic_dev['name']}")

    # ------------------------------------------------------ auto-selection --
    def _loopback_for_current_default(self, wasapi):
        """The loopback device matching whatever Windows currently renders to."""
        try:
            default_speakers = self.pa.get_device_info_by_index(
                wasapi["defaultOutputDevice"])
        except Exception:
            return None
        for d in self.pa.get_loopback_device_info_generator():
            if default_speakers["name"] in d["name"]:
                return d
        return None

    def _best_auto_mic(self, wasapi):
        """Windows' default input device -- unless it's the same physical
        device as the loopback source, in which case opening it would force
        a Bluetooth headset from its stereo (A2DP) profile into narrowband
        Hands-Free mode to support simultaneous mic+speaker use, which
        typically makes loopback on that same device go silent or silently
        fall back to mic-only (exactly the "other person's audio is missing"
        failure). Prefer a different input device when one exists."""
        default_idx = wasapi.get("defaultInputDevice", -1)
        default_mic = (self.pa.get_device_info_by_index(default_idx)
                       if default_idx >= 0 else self.pa.get_default_input_device_info())
        if not (self.loop_dev and _same_physical_device(default_mic["name"], self.loop_dev["name"])):
            return default_mic
        for i, name in list_input_devices_raw(self.pa):
            if not _same_physical_device(name, self.loop_dev["name"]):
                d = self.pa.get_device_info_by_index(i)
                print(f"  (avoiding {default_mic['name']} as mic -- it would "
                      f"force {self.loop_dev['name']} into hands-free mode "
                      f"and likely break system-audio capture; using {name})")
                return d
        return default_mic  # no alternative input device exists

    def _loop_cb(self, in_data, frame_count, time_info, status):
        self._loop_q.put(in_data)
        return (None, pyaudio.paContinue)

    def _mic_cb(self, in_data, frame_count, time_info, status):
        self._mic_q.put(in_data)
        return (None, pyaudio.paContinue)

    def _open_loop_stream(self, dev):
        lr = int(dev["defaultSampleRate"])
        lc = int(dev["maxInputChannels"])
        stream = self.pa.open(
            format=pyaudio.paInt16, channels=lc, rate=lr, input=True,
            input_device_index=dev["index"],
            frames_per_buffer=int(lr * FRAME_MS / 1000),
            stream_callback=self._loop_cb)
        self._loop_fmt = (lr, lc)
        return stream

    def _open_mic_stream(self, dev):
        mr = int(dev["defaultSampleRate"])
        mc = int(dev["maxInputChannels"])
        stream = self.pa.open(
            format=pyaudio.paInt16, channels=mc, rate=mr, input=True,
            input_device_index=dev["index"],
            frames_per_buffer=int(mr * FRAME_MS / 1000),
            stream_callback=self._mic_cb)
        self._mic_fmt = (mr, mc)
        return stream

    def start(self):
        self._streams.append(self._open_loop_stream(self.loop_dev))
        if self.use_mic:
            self._streams.append(self._open_mic_stream(self.mic_dev))

        threading.Thread(target=self._mix_loop, daemon=True).start()
        if self._auto_loop or (self.use_mic and self._auto_mic):
            threading.Thread(target=self._follow_default_devices, daemon=True).start()

    # ------------------------------------------------- live device tracking --
    def _follow_default_devices(self):
        """While a source was left on "auto", keep tracking Windows' actual
        default and hot-swap to it if it changes -- e.g. the user connects
        headphones, or a Bluetooth device reconnects, after recording has
        already started. Only touches sources the user didn't pin explicitly."""
        while not self._stop.is_set():
            time.sleep(self.DEVICE_RECHECK_S)
            if self._stop.is_set():
                break
            try:
                wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                if self._auto_loop:
                    new_loop = self._loopback_for_current_default(wasapi)
                    if new_loop and new_loop["index"] != self.loop_dev["index"]:
                        self._swap_loop_stream(new_loop)
                if self.use_mic and self._auto_mic:
                    new_mic = self._best_auto_mic(wasapi)
                    if new_mic and new_mic["index"] != self.mic_dev["index"]:
                        self._swap_mic_stream(new_mic)
            except Exception:
                pass

    def _swap_loop_stream(self, new_dev):
        try:
            old = self._streams[0]
            old.stop_stream()
            old.close()
        except Exception:
            pass
        self.loop_dev = new_dev
        self._loop_q = queue.Queue()  # drop any in-flight frames in the old format
        self._streams[0] = self._open_loop_stream(new_dev)
        self.last_loop_ts = time.time()
        print(f"  System audio switched -> {new_dev['name']}")

    def _swap_mic_stream(self, new_dev):
        try:
            old = self._streams[1]
            old.stop_stream()
            old.close()
        except Exception:
            pass
        self.mic_dev = new_dev
        self._mic_q = queue.Queue()
        self._streams[1] = self._open_mic_stream(new_dev)
        self.last_mic_ts = time.time()
        print(f"  Microphone switched -> {new_dev['name']}")

    def _mix_loop(self):
        """Pull from both queues, resample to 16k mono, sum, emit fixed frames.

        Either source can legitimately go quiet while the other keeps producing
        (a Bluetooth sink suspending itself when idle, a mic losing its device).
        Waiting on one queue before ever draining the other used to mean a dead
        source silently blocked the live one too -- track each source's last
        delivery and fall back to the live one alone once the other stalls.
        """
        loop_buf = np.zeros(0, dtype=np.float32)
        mic_buf = np.zeros(0, dtype=np.float32)
        carry = np.zeros(0, dtype=np.float32)
        last_loop = last_mic = time.time()
        # Public (not just local) so callers -- e.g. app.py's recording loop --
        # can tell a sustained one-sided silence from a brief stall and warn
        # the user that the *other* source has gone quiet, instead of the
        # recording just quietly degrading to one side with no sign of it.
        self.last_loop_ts = last_loop
        self.last_mic_ts = last_mic
        STALL_S = 1.0
        while not self._stop.is_set():
            drained = False
            while not self._loop_q.empty():
                loop_buf = np.concatenate(
                    [loop_buf, to_mono_16k(self._loop_q.get(), *self._loop_fmt)])
                last_loop = self.last_loop_ts = time.time()
                drained = True
            if self.use_mic:
                while not self._mic_q.empty():
                    mic_buf = np.concatenate(
                        [mic_buf, to_mono_16k(self._mic_q.get(), *self._mic_fmt)])
                    last_mic = self.last_mic_ts = time.time()
                    drained = True
            if not drained:
                time.sleep(0.02)
                continue

            if self.use_mic:
                now = time.time()
                loop_stalled = (now - last_loop) > STALL_S and len(mic_buf) > 0
                mic_stalled = (now - last_mic) > STALL_S and len(loop_buf) > 0
                if loop_stalled:
                    mixed = mic_buf * 0.85
                    mic_buf = np.zeros(0, dtype=np.float32)
                    loop_buf = np.zeros(0, dtype=np.float32)
                elif mic_stalled:
                    mixed = loop_buf * 0.85
                    loop_buf = np.zeros(0, dtype=np.float32)
                    mic_buf = np.zeros(0, dtype=np.float32)
                else:
                    n = min(len(loop_buf), len(mic_buf))
                    if n == 0:
                        continue
                    # Weighted sum keeps both voices audible without instant
                    # clipping when the remote speaker and you talk at once.
                    mixed = loop_buf[:n] * 0.85 + mic_buf[:n] * 0.85
                    loop_buf, mic_buf = loop_buf[n:], mic_buf[n:]
            else:
                mixed = loop_buf
                loop_buf = np.zeros(0, dtype=np.float32)

            # Soft limit instead of hard clipping (clipping distorts -> mis-hears).
            peak = float(np.max(np.abs(mixed))) if len(mixed) else 0.0
            if peak > 1.0:
                mixed = mixed / peak
            carry = np.concatenate([carry, mixed])
            while len(carry) >= FRAME_SAMPLES:
                self.out_q.put(carry[:FRAME_SAMPLES].copy())
                carry = carry[FRAME_SAMPLES:]

    def stop(self):
        self._stop.set()
        for s in self._streams:
            try:
                s.stop_stream(); s.close()
            except Exception:
                pass
        self.pa.terminate()


def main():
    ap = argparse.ArgumentParser(description="Local real-time meeting transcriber")
    ap.add_argument("--model", default="base",
                    help="tiny|base|small|medium (bigger = better & slower)")
    ap.add_argument("--language", default=None,
                    help="lock language e.g. 'en' (default: auto-detect)")
    ap.add_argument("--no-mic", action="store_true",
                    help="don't include your microphone, only what you hear")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()

    # Windows consoles default to cp1252 and crash on non-Latin text; force UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.list_devices:
        list_devices()
        return

    from faster_whisper import WhisperModel

    print(f"Loading Whisper model '{args.model}' (CPU, int8)...")
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = f"C:/Users/monny/meet-transcriber/transcript_{stamp}.txt"
    out_file = open(out_path, "w", encoding="utf-8")
    print(f"Transcript -> {out_path}\n")

    print("Audio sources:")
    cap = AudioCapture(use_mic=not args.no_mic)
    cap.start()

    vad = webrtcvad.Vad(2)  # 0=lenient .. 3=aggressive
    seg_q = queue.Queue()
    stop = threading.Event()

    def transcriber():
        while not stop.is_set():
            try:
                audio = seg_q.get(timeout=0.5)
            except queue.Empty:
                continue
            segments, _ = model.transcribe(
                audio, language=args.language, beam_size=1,
                vad_filter=False, condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
            if text:
                ts = dt.datetime.now().strftime("%H:%M:%S")
                line = f"[{ts}] {text}"
                print(line, flush=True)
                out_file.write(line + "\n")
                out_file.flush()

    threading.Thread(target=transcriber, daemon=True).start()

    print("\nListening... (start your Google Meet; press Ctrl+C to stop)\n")
    collected = []
    speech_started = False
    trailing_silence_ms = 0
    silence_limit = SILENCE_TAIL_MS

    try:
        while True:
            frame = cap.out_q.get()
            pcm16 = (frame * 32767).astype(np.int16).tobytes()
            is_speech = vad.is_speech(pcm16, TARGET_RATE)

            if is_speech:
                collected.append(frame)
                speech_started = True
                trailing_silence_ms = 0
            elif speech_started:
                collected.append(frame)
                trailing_silence_ms += FRAME_MS

            dur_s = len(collected) * FRAME_MS / 1000.0
            end_phrase = speech_started and (
                trailing_silence_ms >= silence_limit or dur_s >= MAX_PHRASE_S)
            if end_phrase:
                if dur_s * 1000 >= MIN_PHRASE_MS:
                    seg_q.put(np.concatenate(collected))
                collected = []
                speech_started = False
                trailing_silence_ms = 0
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop.set()
        cap.stop()
        time.sleep(0.3)
        out_file.close()
        print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
