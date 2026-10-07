"""Windows 'launch at startup' via the per-user HKCU Run key.

This matches how the app is already installed: build_tools/installer.iss sets
PrivilegesRequired=lowest, i.e. a per-user install with no admin rights -- HKCU
is the per-user equivalent (HKLM would need admin and wouldn't match). Uses
only the stdlib winreg module -- a Startup-folder .lnk shortcut would need
pywin32 to construct correctly, which isn't a dependency today.

Off by default: this only ever runs when the user explicitly flips the
Settings toggle. Nothing about Windows boot behavior changes on its own.
"""

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "LocalMeetTranscriber"


def _exe_command():
    """The exact command line to register -- must match what set_enabled(True)
    wrote, so is_enabled() can tell "ours" apart from something else with the
    same value name."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --start-hidden'
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return f'"{sys.executable}" "{main_py}" --start-hidden'


def is_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return value == _exe_command()
    except OSError:
        return False


def set_enabled(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _exe_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
