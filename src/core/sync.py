"""Audio-video synchronization manager.

Handles PTS alignment between enhanced audio and video playback,
dynamic buffer management, seek recovery, and seamless enhance toggle.
"""

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class SyncState(Enum):
    IDLE = "idle"
    SYNCED = "synced"
    RESYNCING = "resyncing"
    FALLBACK = "fallback"


@dataclass
class SyncStatus:
    state: SyncState = SyncState.IDLE
    drift_ms: float = 0.0
    enhanced_active: bool = False
    fallback_reason: str = ""


# Maximum acceptable A/V drift before triggering resync
MAX_DRIFT_MS = 80.0
# Drift threshold for soft correction (speed adjustment)
SOFT_DRIFT_MS = 30.0
# How often to check sync (seconds)
SYNC_CHECK_INTERVAL = 0.5


class SyncManager:
    """Manages audio-video synchronization for enhanced audio playback.

    Strategy:
    - mpv handles video + original audio natively with its own sync
    - When enhanced audio replaces original, we track the PTS offset
    - On seek: record the target position, let mpv seek video, then
      align enhanced audio to the same position
    - On drift: apply micro speed corrections to bring audio back in sync
    - On failure: seamlessly fall back to original audio
    """

    def __init__(self):
        self._state = SyncState.IDLE
        self._lock = threading.Lock()
        self._enhanced_active = False
        self._original_audio_url: Optional[str] = None
        self._enhanced_audio_path: Optional[str] = None
        self._video_position_fn: Optional[Callable[[], float]] = None
        self._audio_position_fn: Optional[Callable[[], float]] = None
        self._seek_fn: Optional[Callable[[float], None]] = None
        self._switch_audio_fn: Optional[Callable[[str], None]] = None
        self._set_speed_fn: Optional[Callable[[float], None]] = None
        self._get_speed_fn: Optional[Callable[[], float]] = None
        self._status_callback: Optional[Callable[[SyncStatus], None]] = None

        self._base_speed = 1.0
        self._correction_active = False
        self._last_seek_time = 0.0
        self._seek_cooldown = 1.0  # seconds after seek before checking drift
        self._pts_offset = 0.0  # offset between enhanced audio and video timeline

        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def configure(self, *,
                  video_position_fn: Callable[[], float],
                  audio_position_fn: Callable[[], float],
                  seek_fn: Callable[[float], None],
                  switch_audio_fn: Callable[[str], None],
                  set_speed_fn: Callable[[float], None],
                  get_speed_fn: Callable[[], float],
                  status_callback: Optional[Callable[[SyncStatus], None]] = None):
        """Wire up the sync manager to player functions."""
        self._video_position_fn = video_position_fn
        self._audio_position_fn = audio_position_fn
        self._seek_fn = seek_fn
        self._switch_audio_fn = switch_audio_fn
        self._set_speed_fn = set_speed_fn
        self._get_speed_fn = get_speed_fn
        self._status_callback = status_callback

    def set_original_audio(self, url: str):
        """Store the original audio URL for fallback."""
        self._original_audio_url = url

    def activate_enhanced(self, enhanced_path: str, position_at_switch: float = 0.0):
        """Switch to enhanced audio and start sync monitoring.

        Args:
            enhanced_path: path to enhanced WAV file
            position_at_switch: video position (seconds) when switch happens
        """
        with self._lock:
            self._enhanced_audio_path = enhanced_path
            self._enhanced_active = True
            self._pts_offset = position_at_switch
            self._state = SyncState.SYNCED

        if self._switch_audio_fn:
            self._switch_audio_fn(enhanced_path)

        self._start_monitor()
        self._emit_status()
        logger.info(f"Enhanced audio activated at position {position_at_switch:.1f}s")

    def deactivate_enhanced(self):
        """Switch back to original audio."""
        self._stop_monitor()
        with self._lock:
            self._enhanced_active = False
            self._state = SyncState.IDLE
            self._correction_active = False

        if self._original_audio_url and self._switch_audio_fn:
            self._switch_audio_fn(self._original_audio_url)

        self._restore_speed()
        self._emit_status()
        logger.info("Switched back to original audio")

    def notify_seek(self, target_position: float):
        """Called when user seeks. Records timing to suppress drift checks."""
        with self._lock:
            self._last_seek_time = time.monotonic()
            if self._correction_active:
                self._correction_active = False
                self._restore_speed()

    def notify_speed_change(self, speed: float):
        """Called when user changes playback speed."""
        self._base_speed = speed
        if not self._correction_active:
            pass  # speed already set by user

    def fallback_to_original(self, reason: str = ""):
        """Emergency fallback to original audio."""
        logger.warning(f"Falling back to original audio: {reason}")
        self._stop_monitor()
        with self._lock:
            self._state = SyncState.FALLBACK
            self._enhanced_active = False
            self._correction_active = False

        if self._original_audio_url and self._switch_audio_fn:
            self._switch_audio_fn(self._original_audio_url)

        self._restore_speed()
        self._emit_status(fallback_reason=reason)

    @property
    def is_enhanced_active(self) -> bool:
        return self._enhanced_active

    @property
    def state(self) -> SyncState:
        return self._state

    def cleanup(self):
        self._stop_monitor()

    def _start_monitor(self):
        """Start the sync monitoring thread."""
        self._stop_monitor()
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True
        )
        self._monitor_thread.start()

    def _stop_monitor(self):
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=1.0)
        self._monitor_thread = None

    def _monitor_loop(self):
        """Periodically check A/V drift and apply corrections."""
        while not self._stop_event.is_set():
            self._stop_event.wait(SYNC_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break

            with self._lock:
                if not self._enhanced_active:
                    break
                if time.monotonic() - self._last_seek_time < self._seek_cooldown:
                    continue

            try:
                self._check_drift()
            except Exception as e:
                logger.warning(f"Sync check error: {e}")

    def _check_drift(self):
        """Measure A/V drift and apply correction if needed."""
        if not self._video_position_fn or not self._audio_position_fn:
            return

        try:
            video_pos = self._video_position_fn()
            audio_pos = self._audio_position_fn()
        except Exception:
            return

        if video_pos is None or audio_pos is None:
            return
        if video_pos <= 0 or audio_pos <= 0:
            return

        drift_ms = (audio_pos - video_pos) * 1000.0

        action = None  # "hard_resync", "soft_correct", or "restore"
        target_speed = self._base_speed

        with self._lock:
            if abs(drift_ms) > MAX_DRIFT_MS:
                self._state = SyncState.RESYNCING
                self._correction_active = False
                action = "hard_resync"
            elif abs(drift_ms) > SOFT_DRIFT_MS:
                if not self._correction_active:
                    self._correction_active = True
                correction = 1.0 + (0.02 if drift_ms < 0 else -0.02)
                target_speed = self._base_speed * correction
                action = "soft_correct"
            else:
                if self._correction_active:
                    self._correction_active = False
                    action = "restore"
                self._state = SyncState.SYNCED

        # Execute actions outside the lock to avoid deadlock
        if action == "hard_resync":
            logger.info(f"Hard resync: drift={drift_ms:.0f}ms, seeking audio to video pos")
            if self._set_speed_fn:
                self._set_speed_fn(self._base_speed)
            if self._seek_fn:
                self._seek_fn(video_pos)
            with self._lock:
                self._last_seek_time = time.monotonic()
                self._state = SyncState.SYNCED
        elif action == "soft_correct":
            if self._set_speed_fn:
                self._set_speed_fn(target_speed)
        elif action == "restore":
            self._restore_speed()

        self._emit_status()

    def _restore_speed(self):
        if self._set_speed_fn:
            self._set_speed_fn(self._base_speed)

    def _emit_status(self, fallback_reason: str = ""):
        if self._status_callback:
            status = SyncStatus(
                state=self._state,
                drift_ms=0.0,
                enhanced_active=self._enhanced_active,
                fallback_reason=fallback_reason,
            )
            self._status_callback(status)
