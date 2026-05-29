import json
import threading
from pathlib import Path

DEFAULT_CONFIG = {
    "audio_exclusive": True,
    "audio_device": "auto",
    "enhance_enabled": False,
    "enhance_mode": "realtime",  # "realtime" or "quality"
    "output_sample_rate": 48000,
    "cookie_file": "",
    "cookie_browser": "edge",  # e.g. "chrome", "firefox", "edge"
    "volume": 100,
    "last_url": "",
    "thumbnail_mode": False,
    "thumbnail_size": 80,
    "llm_providers": [],
    "llm_default_provider": "",
    "subtitle_language": "zh",
    "subtitle_model": "openai/whisper-large-v3",
}

CONFIG_PATH = Path.home() / ".ventiplayer" / "config.json"

_SAVE_DELAY = 1.0  # seconds


class Settings:
    def __init__(self):
        self._data = dict(DEFAULT_CONFIG)
        self._dirty = False
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._data.update(saved)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        self._dirty = False

    def get(self, key: str):
        return self._data.get(key, DEFAULT_CONFIG.get(key))

    def set(self, key: str, value):
        self._data[key] = value
        self._schedule_save()

    def _schedule_save(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._dirty = True
            self._timer = threading.Timer(_SAVE_DELAY, self.save)
            self._timer.daemon = True
            self._timer.start()

    def flush(self):
        """Force immediate save if dirty (call on app exit)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._dirty:
            self.save()
