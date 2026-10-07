"""Dev-only launcher: forces MEET_BROWSER=1 before running the app entry point,
for fast browser-based iteration without the desktop shell (tray/hotkey/native
window). Not part of the packaged app -- safe to delete."""
import os
os.environ["MEET_BROWSER"] = "1"
import app
app.main()
