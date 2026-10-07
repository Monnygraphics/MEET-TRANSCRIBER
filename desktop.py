"""
Desktop shell for Local Meet Transcriber: system tray icon, a global hotkey
to start/stop recording from anywhere, and a small floating "HUD" window
that shows recording status without needing the full app window open.

This mirrors the "background-first" UX of tools like Handy (cjpais/handy):
the app can live in the tray, be triggered by a hotkey, and show only a
compact overlay while recording — the full window is optional.
"""

import os
import threading
import time

import pystray
from PIL import Image, ImageDraw

import app as appmod

HOTKEY = "<ctrl>+<alt>+r"   # overwritten from settings.json in start()
_hotkey_listener = None


# ---------------------------------------------------------------- icons ----
def _tray_image(recording=False):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top = (239, 68, 68) if recording else (109, 139, 255)
    bot = (220, 38, 38) if recording else (52, 211, 153)
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        d.line([(0, y), (size, y)], fill=c + (255,))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([2, 2, size - 2, size - 2], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([26, 16, 38, 36], radius=6, fill=(255, 255, 255, 255))
    d.arc([18, 22, 46, 44], start=20, end=160, fill=(255, 255, 255, 255), width=3)
    d.line([32, 42, 32, 50], fill=(255, 255, 255, 255), width=3)
    return out


_ICON_IDLE = _tray_image(False)
_ICON_REC = _tray_image(True)


# --------------------------------------------------------------- js api ----
class State:
    main_win = None
    hud_win = None
    tray_icon = None
    quitting = False
    # pywebview's hide()/show() don't update a queryable "is visible" flag on
    # every backend, so we track it ourselves through our own wrappers below.
    main_visible = True
    hud_visible = False


# Shared across the whole process: created once at import time so the HUD
# window's js_api (bound before desktop.start() runs) and the tray/hotkey
# logic (wired up inside desktop.start()) always refer to the same object.
state = State()


class HudApi:
    """Exposed to the HUD window's JS as window.pywebview.api.*"""

    def show_main(self):
        _show_main()

    def hide_hud(self):
        _hide_hud()


# ------------------------------------------------------------- actions -----
def _show_main():
    try:
        state.main_win.show()
        state.main_win.restore()
        state.main_visible = True
    except Exception:
        pass


def _hide_main():
    try:
        state.main_win.hide()
        state.main_visible = False
    except Exception:
        pass


def _toggle_show_main():
    _hide_main() if state.main_visible else _show_main()


def _toggle_record():
    _, active = appmod.current_status()
    if active:
        appmod.stop_current_session()
    else:
        appmod.start_live_session()   # sensible defaults: small/auto/mic+denoise on
        _show_hud()


def _show_hud():
    try:
        state.hud_win.show()
        state.hud_visible = True
    except Exception:
        pass


def _hide_hud():
    try:
        state.hud_win.hide()
        state.hud_visible = False
    except Exception:
        pass


def _quit():
    state.quitting = True
    try:
        state.tray_icon.stop()
    except Exception:
        pass
    try:
        state.hud_win.destroy()
    except Exception:
        pass
    try:
        state.main_win.destroy()
    except Exception:
        pass
    os._exit(0)


# ----------------------------------------------------------- tray menu -----
def _build_tray():
    def label_show(item):
        return "Hide window" if state.main_visible else "Show window"

    def label_record(item):
        _, active = appmod.current_status()
        return "Stop recording" if active else "Start recording"

    menu = pystray.Menu(
        pystray.MenuItem(label_show, lambda i: _toggle_show_main(), default=True),
        pystray.MenuItem(label_record, lambda i: _toggle_record()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item:
                         f"{'Hold' if PUSH_TO_TALK else 'Press'} {_pretty_hotkey()}",
                         None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda i: _quit()),
    )
    icon = pystray.Icon("meet-transcriber", _ICON_IDLE,
                        "Local Meet Transcriber", menu)
    state.tray_icon = icon
    return icon


# -------------------------------------------------------- watcher loop ----
def _notify_stopped(sess):
    """Tray balloon on every stop — the one signal that still reaches the user
    even with the main window hidden (see start_hidden), and the actual fix for
    a recording being silently cut short (e.g. a hotkey colliding with browser
    refresh) with no visible sign anything went wrong."""
    if state.tray_icon is None or sess is None or getattr(sess, "_discard", False):
        return
    try:
        rec_start = getattr(sess, "rec_start", None)
        if rec_start is None:
            return  # not a live Session (e.g. FileSession) -- nothing "stopped" to report
        dur = max(0, time.monotonic() - rec_start)
        m, s = divmod(int(dur), 60)
        count = len(sess.lines)
        state.tray_icon.notify(
            f"Recording stopped after {m}:{s:02d} · "
            f"{count} line{'s' if count != 1 else ''} captured.",
            "Local Meet Transcriber")
    except Exception:
        pass


def _watch_status():
    """Poll session state to: swap the tray icon, auto-show/hide the HUD,
    and keep the tray menu labels fresh."""
    last_active = False
    idle_since = None
    while not state.quitting:
        try:
            _, active = appmod.current_status()
            if active != last_active:
                if state.tray_icon:
                    state.tray_icon.icon = _ICON_REC if active else _ICON_IDLE
                if active:
                    _show_hud()
                    idle_since = None
                else:
                    idle_since = time.time()
                    _notify_stopped(appmod.session)
                last_active = active
            # Auto-hide the HUD a couple seconds after a session finishes
            # (gives the user a moment to see the final status).
            if not active and idle_since and time.time() - idle_since > 2.5:
                _hide_hud()
                idle_since = None
        except Exception:
            pass
        time.sleep(1.0)


# ------------------------------------------------------------- hotkey ------
# A single low-level press/release listener drives both the record combo
# (toggle OR push-to-talk, per setting) and a separate cancel combo — rather
# than pynput's higher-level GlobalHotKeys, which only reports completed
# chords and can't distinguish "held" from "tapped".
CANCEL_HOTKEY = "<esc>"
PUSH_TO_TALK = False

_MOD_ALIASES = {}   # populated in _init_pynput_maps()
_pressed = set()
_record_combo = set()
_cancel_combo = set()
_record_edge = False
_cancel_edge = False


def _pretty_hotkey(combo=None):
    return (combo or HOTKEY).replace("<", "").replace(">", "").replace("+", " + ").title()


def _init_pynput_maps():
    from pynput import keyboard as kb
    global _MOD_ALIASES
    _MOD_ALIASES = {
        kb.Key.ctrl_l: kb.Key.ctrl, kb.Key.ctrl_r: kb.Key.ctrl,
        kb.Key.alt_l: kb.Key.alt, kb.Key.alt_r: kb.Key.alt,
        kb.Key.alt_gr: kb.Key.alt,
        kb.Key.shift_l: kb.Key.shift, kb.Key.shift_r: kb.Key.shift,
    }
    return kb


def _normalize_key(key):
    return _MOD_ALIASES.get(key, key)


def _parse_combo(kb, combo_str):
    keys = set()
    for part in (combo_str or "").replace(" ", "").split("+"):
        name = part.strip("<>").lower()
        if not name:
            continue
        named = {"ctrl": kb.Key.ctrl, "alt": kb.Key.alt, "shift": kb.Key.shift,
                 "cmd": kb.Key.cmd, "esc": kb.Key.esc, "space": kb.Key.space,
                 "tab": kb.Key.tab, "enter": kb.Key.enter}
        if name in named:
            keys.add(named[name])
        elif len(name) == 1:
            keys.add(kb.KeyCode.from_char(name))
        else:
            k = getattr(kb.Key, name, None)
            if k:
                keys.add(k)
    return keys


def _on_key_press(key):
    global _record_edge, _cancel_edge
    nk = _normalize_key(key)
    _pressed.add(nk)
    if _record_combo and _record_combo.issubset(_pressed) and not _record_edge:
        _record_edge = True
        if PUSH_TO_TALK:
            _, active = appmod.current_status()
            if not active:
                appmod.start_live_session()
                _show_hud()
        else:
            _toggle_record()
    if _cancel_combo and _cancel_combo.issubset(_pressed) and not _cancel_edge:
        _cancel_edge = True
        _, active = appmod.current_status()
        if active:
            appmod.cancel_current_session()
            _hide_hud()


def _on_key_release(key):
    global _record_edge, _cancel_edge
    nk = _normalize_key(key)
    if (PUSH_TO_TALK and _record_combo and nk in _record_combo
            and _record_combo.issubset(_pressed)):
        _, active = appmod.current_status()
        if active:
            appmod.stop_current_session()
    _pressed.discard(nk)
    if not (_record_combo and _record_combo.issubset(_pressed)):
        _record_edge = False
    if not (_cancel_combo and _cancel_combo.issubset(_pressed)):
        _cancel_edge = False


def _start_hotkey():
    try:
        kb = _init_pynput_maps()
        listener = kb.Listener(on_press=_on_key_press, on_release=_on_key_release)
        listener.start()
        return listener
    except Exception as e:
        print("Global hotkey unavailable:", e)
        return None


def apply_hotkey(combo, cancel_combo=None, ptt=None):
    """(Re)configure the record/cancel combos and mode without restarting the
    listener — called on startup and live from the Settings panel."""
    global HOTKEY, CANCEL_HOTKEY, PUSH_TO_TALK
    global _record_combo, _cancel_combo, _pressed, _record_edge, _cancel_edge
    try:
        # Cheap once pynput.keyboard is already imported — this just reuses
        # the cached module rather than re-executing it.
        kb = _init_pynput_maps()
    except Exception:
        return
    if combo:
        HOTKEY = combo
        _record_combo = _parse_combo(kb, combo)
    if cancel_combo is not None:
        CANCEL_HOTKEY = cancel_combo
        _cancel_combo = _parse_combo(kb, cancel_combo)
    if ptt is not None:
        PUSH_TO_TALK = bool(ptt)
    _pressed.clear()
    _record_edge = False
    _cancel_edge = False


# --------------------------------------------------------------- setup -----
def start(main_win, hud_win, start_hidden=False):
    """Called once pywebview's event loop is running. Wires up the tray,
    hotkey, and HUD auto-show/hide. Never blocks."""
    state.main_win = main_win
    state.hud_win = hud_win
    state.main_visible = not start_hidden
    settings = appmod.load_settings()

    def on_main_closing():
        if state.quitting:
            return True
        _hide_main()
        return False
    main_win.events.closing += on_main_closing

    def on_hud_closing():
        if state.quitting:
            return True
        _hide_hud()
        return False
    hud_win.events.closing += on_hud_closing

    icon = _build_tray()
    threading.Thread(target=icon.run, daemon=True).start()
    threading.Thread(target=_watch_status, daemon=True).start()
    listener = _start_hotkey()
    apply_hotkey(settings.get("hotkey", HOTKEY),
                settings.get("cancel_hotkey", CANCEL_HOTKEY),
                settings.get("push_to_talk", PUSH_TO_TALK))
    return state
