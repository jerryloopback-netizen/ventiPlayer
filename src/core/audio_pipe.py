"""Audio pipeline: decode stream → chunk → enhance → output.

Handles both real-time (FastWave streaming) and quality (AudioSR batch) modes.
Uses PyAV for stream decoding and feeds enhanced audio back to mpv.
"""

import atexit
import logging
import tempfile
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

CHUNK_DURATION_S = 5.0
PREBUFFER_CHUNKS = 2

_TEMP_PREFIX = "ventiplayer_"


def _cleanup_stale_temp_dirs():
    """Remove leftover ventiplayer temp dirs from previous crashed sessions."""
    import shutil
    tmp_root = Path(tempfile.gettempdir())
    for d in tmp_root.glob(f"{_TEMP_PREFIX}*"):
        if d.is_dir():
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


_stale_cleaned = False


class PipelineState(Enum):
    IDLE = "idle"
    DECODING = "decoding"
    ENHANCING = "enhancing"
    READY = "ready"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class PipelineStatus:
    state: PipelineState = PipelineState.IDLE
    progress: float = 0.0
    message: str = ""
    enhanced_file: Optional[str] = None
    enhanced_duration_s: float = 0.0
    recoverable: bool = False  # True if error is recoverable (can fallback)


class AudioPipeline:
    """Manages the audio decode → enhance → output flow.

    Real-time mode (FastWave):
        Decodes audio stream in chunks, enhances each chunk on GPU,
        writes enhanced chunks to a temp file that mpv plays.

    Quality mode (AudioSR):
        Decodes full audio, runs AudioSR on the whole thing,
        saves to temp file for mpv playback.
    """

    def __init__(self, enhancer):
        """
        Args:
            enhancer: src.core.enhancer.Enhancer instance
        """
        global _stale_cleaned
        if not _stale_cleaned:
            _stale_cleaned = True
            _cleanup_stale_temp_dirs()

        self._enhancer = enhancer
        self._worker_thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._generation = 0
        self._status = PipelineStatus()
        self._status_lock = threading.Lock()
        self._status_callback: Optional[Callable[[PipelineStatus], None]] = None
        self._temp_dir = tempfile.mkdtemp(prefix=_TEMP_PREFIX)
        atexit.register(self.cleanup)

    def set_status_callback(self, callback: Callable[[PipelineStatus], None]):
        self._status_callback = callback

    @property
    def status(self) -> PipelineStatus:
        with self._status_lock:
            return PipelineStatus(
                state=self._status.state,
                progress=self._status.progress,
                message=self._status.message,
                enhanced_file=self._status.enhanced_file,
                enhanced_duration_s=self._status.enhanced_duration_s,
                recoverable=self._status.recoverable,
            )

    def _update_status(self, **kwargs):
        with self._status_lock:
            for k, v in kwargs.items():
                setattr(self._status, k, v)
        if self._status_callback:
            self._status_callback(self.status)

    def start_quality_enhance(self, audio_url: str, http_headers: dict = None):
        """Start quality (AudioSR) enhancement in background thread."""
        self.cancel()
        self._cancel.clear()
        self._generation += 1
        gen = self._generation
        self._worker_thread = threading.Thread(
            target=self._quality_worker,
            args=(audio_url, http_headers, gen),
            daemon=True,
        )
        self._worker_thread.start()

    def start_realtime_enhance(self, audio_url: str, http_headers: dict = None):
        """Start real-time (FastWave) enhancement in background thread."""
        self.cancel()
        self._cancel.clear()
        self._generation += 1
        gen = self._generation
        self._worker_thread = threading.Thread(
            target=self._realtime_worker,
            args=(audio_url, http_headers, gen),
            daemon=True,
        )
        self._worker_thread.start()

    def cancel(self):
        """Cancel ongoing enhancement."""
        self._cancel.set()
        if self._worker_thread and self._worker_thread.is_alive():
            # Don't block waiting for worker — it's a daemon thread and
            # may be stuck in a long model inference call
            self._worker_thread.join(timeout=0.5)
        self._worker_thread = None
        self._update_status(state=PipelineState.IDLE, progress=0.0, message="")

    def _decode_full_audio(self, audio_url: str, http_headers: dict = None) -> tuple:
        """Decode full audio stream to numpy array using PyAV.

        Returns:
            (audio_data: np.ndarray float32, sample_rate: int)
        """
        import av

        self._update_status(state=PipelineState.DECODING, progress=0.0,
                           message="正在解码音频流...")

        options = {}
        if http_headers:
            full_headers = dict(http_headers)
            if "User-Agent" not in full_headers:
                full_headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            options["user_agent"] = full_headers["User-Agent"]
            if "Referer" in full_headers:
                options["referer"] = full_headers["Referer"]

        container = av.open(audio_url, options=options)
        audio_stream = container.streams.audio[0]
        sample_rate = audio_stream.rate

        frames = []
        total_samples = 0

        for packet in container.demux(audio_stream):
            if self._cancel.is_set():
                container.close()
                return None, 0

            for frame in packet.decode():
                arr = frame.to_ndarray(format='fltp')
                if arr.ndim > 1:
                    arr = arr.mean(axis=0)
                frames.append(arr.astype(np.float32))
                total_samples += len(arr)

        container.close()

        if not frames:
            return None, 0

        audio_data = np.concatenate(frames)
        self._update_status(progress=0.3, message=f"解码完成 ({total_samples} samples @ {sample_rate}Hz)")
        return audio_data, sample_rate

    def _quality_worker(self, audio_url: str, http_headers: dict = None, gen: int = 0):
        """Worker thread for quality (AudioSR) mode."""
        try:
            audio_data, sample_rate = self._decode_full_audio(audio_url, http_headers)
            if audio_data is None:
                return

            if self._cancel.is_set() or gen != self._generation:
                return

            self._update_status(state=PipelineState.ENHANCING, progress=0.3,
                               message="AudioSR 增强中...")

            def progress_cb(p):
                if self._cancel.is_set():
                    raise InterruptedError("Enhancement cancelled")
                overall = 0.3 + p * 0.6
                self._update_status(progress=overall,
                                   message=f"AudioSR 增强中... {int(p * 100)}%")

            enhanced = self._enhancer.enhance_full(audio_data, sample_rate,
                                                   progress_callback=progress_cb)

            if self._cancel.is_set() or gen != self._generation:
                return
            import soundfile as sf
            output_sr = self._enhancer._target_sr
            output_path = Path(self._temp_dir) / "enhanced_quality.wav"
            sf.write(str(output_path), enhanced, output_sr, subtype="FLOAT")

            enhanced_duration_s = len(enhanced) / output_sr
            self._update_status(
                state=PipelineState.READY,
                progress=1.0,
                message="增强完成",
                enhanced_file=str(output_path),
                enhanced_duration_s=enhanced_duration_s,
            )

        except InterruptedError:
            logger.info("Quality enhancement cancelled by user")
            return
        except Exception as e:
            if self._cancel.is_set():
                return
            logger.error(f"Quality enhancement failed: {e}")
            self._update_status(state=PipelineState.ERROR,
                               message=f"增强失败: {e}",
                               recoverable=True)

    def _realtime_worker(self, audio_url: str, http_headers: dict = None, gen: int = 0):
        """Worker thread for real-time (FastWave) mode."""
        try:
            import av
            import soundfile as sf

            self._update_status(state=PipelineState.DECODING, progress=0.0,
                               message="正在解码并增强...")

            options = {}
            if http_headers:
                full_headers = dict(http_headers)
                if "User-Agent" not in full_headers:
                    full_headers["User-Agent"] = (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                options["user_agent"] = full_headers["User-Agent"]
                if "Referer" in full_headers:
                    options["referer"] = full_headers["Referer"]

            container = av.open(audio_url, options=options)
            audio_stream = container.streams.audio[0]
            sample_rate = audio_stream.rate
            chunk_samples = int(CHUNK_DURATION_S * sample_rate)

            duration = float(audio_stream.duration * audio_stream.time_base) if audio_stream.duration else 0
            total_chunks_est = max(1, int(duration / CHUNK_DURATION_S)) if duration > 0 else 50

            buffer = np.zeros(0, dtype=np.float32)
            chunk_count = 0
            total_written_samples = 0
            output_sr = self._enhancer._target_sr
            output_path = Path(self._temp_dir) / "enhanced_realtime.wav"

            # Open WAV file in write mode, then append subsequent chunks
            out_file = sf.SoundFile(
                str(output_path), mode='w', samplerate=output_sr,
                channels=1, subtype="FLOAT",
            )

            self._update_status(state=PipelineState.ENHANCING, progress=0.0,
                               message="实时增强中...")

            for packet in container.demux(audio_stream):
                if self._cancel.is_set() or gen != self._generation:
                    break

                for frame in packet.decode():
                    arr = frame.to_ndarray(format='fltp')
                    if arr.ndim > 1:
                        arr = arr.mean(axis=0)
                    buffer = np.concatenate([buffer, arr.astype(np.float32)])

                    while len(buffer) >= chunk_samples:
                        chunk = buffer[:chunk_samples]
                        buffer = buffer[chunk_samples:]

                        try:
                            enhanced = self._enhancer.enhance_chunk(chunk, sample_rate)
                        except RuntimeError as e:
                            if "VRAM" in str(e) or "out of memory" in str(e).lower():
                                out_file.close()
                                container.close()
                                self._update_status(
                                    state=PipelineState.ERROR,
                                    message=f"显存不足，增强中止: {e}",
                                    recoverable=True,
                                )
                                return
                            raise
                        out_file.write(enhanced)
                        out_file.flush()
                        chunk_count += 1
                        total_written_samples += len(enhanced)
                        enhanced_dur = total_written_samples / output_sr

                        progress = min(0.95, chunk_count / total_chunks_est)
                        self._update_status(
                            progress=progress,
                            message=f"已增强 {chunk_count}/{total_chunks_est} 个分块",
                            enhanced_file=str(output_path),
                            enhanced_duration_s=enhanced_dur,
                        )

            # Process remaining buffer
            if len(buffer) > 0 and not self._cancel.is_set() and gen == self._generation:
                try:
                    enhanced = self._enhancer.enhance_chunk(buffer, sample_rate)
                except RuntimeError as e:
                    if "VRAM" in str(e) or "out of memory" in str(e).lower():
                        out_file.close()
                        self._update_status(
                            state=PipelineState.ERROR,
                            message=f"显存不足，增强中止: {e}",
                            recoverable=True,
                        )
                        return
                    raise
                out_file.write(enhanced)
                total_written_samples += len(enhanced)

            out_file.close()
            container.close()

            if self._cancel.is_set() or gen != self._generation:
                return

            if total_written_samples == 0:
                self._update_status(state=PipelineState.ERROR, message="无音频数据")
                return

            enhanced_dur = total_written_samples / output_sr

            self._update_status(
                state=PipelineState.READY,
                progress=1.0,
                message="增强完成",
                enhanced_file=str(output_path),
                enhanced_duration_s=enhanced_dur,
            )

        except Exception as e:
            logger.error(f"Realtime enhancement failed: {e}")
            self._update_status(state=PipelineState.ERROR,
                               message=f"增强失败: {e}",
                               recoverable=True)

    def cleanup(self):
        """Clean up temp files."""
        self.cancel()
        import shutil
        try:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        except Exception:
            pass
