"""Download faster-whisper models into ./models for offline bundling."""
import os
from huggingface_hub import snapshot_download

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "models")
MODELS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
}

for name, repo in MODELS.items():
    dest = os.path.join(OUT, f"faster-whisper-{name}")
    if os.path.exists(os.path.join(dest, "model.bin")):
        print(f"already have {name}, skipping")
        continue
    print(f"Downloading {repo} -> {dest}")
    snapshot_download(repo_id=repo, local_dir=dest,
                      allow_patterns=["*.bin", "*.json", "*.txt", "*.model"])
    print(f"  done: {os.listdir(dest)}")
print("All models ready.")
