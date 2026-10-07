# Override PyInstaller's contrib hook, which calls copy_metadata('webrtcvad').
# The installed distribution is actually 'webrtcvad-wheels', so the stock hook
# raises during analysis. Metadata isn't needed at runtime; collect it under the
# correct name if available, otherwise skip.
from PyInstaller.utils.hooks import copy_metadata

try:
    datas = copy_metadata("webrtcvad-wheels")
except Exception:
    datas = []
