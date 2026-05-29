"""Download AudioSR and FastWave dependencies to local cache.

Run this script once to pre-download all model files needed.
After this, models can run in offline mode.
"""
import os
import sys
import json
import hashlib
from pathlib import Path
from urllib.request import urlretrieve, Request, urlopen

HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"
MODELS_DIR = Path.home() / ".ventiplayer" / "models"
MIRROR = "https://hf-mirror.com"

FASTWAVE_DIR = MODELS_DIR / "fastwave"
FASTWAVE_GDRIVE_ID = "1oNCxrKjgiWsYGW6P49rsI84vFYR5G3m8"


def download_file(repo_id: str, filename: str):
    """Download a file from hf-mirror and place it in the HF cache structure."""
    # HF cache structure: models--{org}--{name}/snapshots/{commit_hash}/{filename}
    repo_dir = HF_CACHE / f"models--{repo_id.replace('/', '--')}"
    refs_dir = repo_dir / "refs"
    snapshots_dir = repo_dir / "snapshots"
    blobs_dir = repo_dir / "blobs"

    refs_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    blobs_dir.mkdir(parents=True, exist_ok=True)

    # Use a fixed fake commit hash
    commit_hash = "0000000000000000000000000000000000000001"
    snapshot_dir = snapshots_dir / commit_hash
    snapshot_dir.mkdir(exist_ok=True)

    # Write refs/main
    (refs_dir / "main").write_text(commit_hash)

    target_path = snapshot_dir / filename
    if target_path.exists():
        print(f"  Already exists: {filename}")
        return target_path

    url = f"{MIRROR}/{repo_id}/resolve/main/{filename}"
    print(f"  Downloading: {url}")
    urlretrieve(url, str(target_path))
    print(f"  Saved: {target_path} ({target_path.stat().st_size / 1024:.0f} KB)")
    return target_path


def download_gdrive(file_id: str, dest: Path):
    """Download a file from Google Drive (handles confirm token for large files)."""
    if dest.exists():
        print(f"  Already exists: {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)

    base_url = "https://drive.google.com/uc?export=download"
    session_url = f"{base_url}&id={file_id}"

    print(f"  Downloading from Google Drive (id={file_id})...")
    try:
        import gdown
        gdown.download(id=file_id, output=str(dest), quiet=False)
    except ImportError:
        print("  [WARNING] gdown not installed. Install with: pip install gdown")
        print(f"  Manual download: https://drive.google.com/file/d/{file_id}/view")
        print(f"  Place the file at: {dest}")
        return

    if dest.exists():
        print(f"  Saved: {dest} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print(f"  [ERROR] Download failed. Please download manually.")


def main():
    print("=== Downloading model files ===\n")

    # 1. FastWave checkpoint
    print("--- FastWave ---")
    fw_ckpt = FASTWAVE_DIR / "checkpoint.pth"
    download_gdrive(FASTWAVE_GDRIVE_ID, fw_ckpt)

    # 2. AudioSR checkpoint
    print("\n--- AudioSR ---")
    audiosr_repo = HF_CACHE / "models--haoheliu--audiosr_basic"
    audiosr_snapshots = audiosr_repo / "snapshots"
    audiosr_found = False
    if audiosr_snapshots.exists():
        for snap in audiosr_snapshots.iterdir():
            if (snap / "pytorch_model.bin").exists():
                audiosr_found = True
                print(f"[OK] AudioSR checkpoint: {(snap / 'pytorch_model.bin').stat().st_size / 1024 / 1024:.0f} MB")
                break
    if not audiosr_found:
        print("[DOWNLOADING] AudioSR checkpoint...")
        download_file("haoheliu/audiosr_basic", "pytorch_model.bin")

    # 3. RoBERTa-base (needed by CLAP tokenizer)
    print("\n--- roberta-base ---")
    roberta_files = [
        "config.json", "tokenizer.json", "vocab.json",
        "merges.txt", "tokenizer_config.json", "special_tokens_map.json",
    ]
    for f in roberta_files:
        download_file("roberta-base", f)

    print("\n=== All downloads complete ===")
    print("Models can now run offline.")


if __name__ == "__main__":
    main()
