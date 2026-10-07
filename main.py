"""
Entry point for Local Meet Transcriber.

This exists so app.py is always imported as a module named "app" (never run
directly as "__main__"). desktop.py does `import app`, and Python caches
modules by name — importing "app" here first means desktop.py's later
`import app` reuses this exact same module object (and its live `session`
state) instead of loading a second, disconnected copy.
"""
import os
import sys
import tempfile
import traceback


def _crash_log(exc_text):
    # Windowed (console=False) builds have no visible stdout/stderr, so an
    # unhandled crash would otherwise vanish silently. Write it somewhere
    # findable instead.
    path = os.path.join(tempfile.gettempdir(), "meet_transcriber_crash.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(exc_text)


if __name__ == "__main__":
    try:
        import app
        app.main()
    except SystemExit:
        raise
    except Exception:
        _crash_log(traceback.format_exc())
        sys.exit(1)
else:
    import app
