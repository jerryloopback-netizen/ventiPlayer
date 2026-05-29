"""AI audio enhancement engine abstraction layer.

Manages device selection (ROCm/DirectML/CPU) and dispatches to model backends.
"""

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

MODELS_DIR = Path.home() / ".ventiplayer" / "models"


class Backend(Enum):
    ROCM = "rocm"
    DIRECTML = "directml"
    CPU = "cpu"


class EnhanceMode(Enum):
    REALTIME = "realtime"
    QUALITY = "quality"


@dataclass
class DeviceInfo:
    backend: Backend
    device_name: str
    vram_mb: int = 0


def detect_device() -> DeviceInfo:
    """Detect best available compute device for inference."""
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
            logger.info(f"ROCm/CUDA device found: {name} ({vram} MB)")
            return DeviceInfo(Backend.ROCM, name, vram)
    except ImportError:
        logger.info("PyTorch not installed, trying DirectML")
    except Exception as e:
        logger.warning(f"PyTorch device detection failed: {e}")

    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        if "DmlExecutionProvider" in providers:
            logger.info("DirectML available via ONNX Runtime")
            return DeviceInfo(Backend.DIRECTML, "DirectML GPU", 0)
    except ImportError:
        logger.info("onnxruntime-directml not installed")
    except Exception as e:
        logger.warning(f"DirectML detection failed: {e}")

    logger.info("Falling back to CPU")
    return DeviceInfo(Backend.CPU, "CPU", 0)


class Enhancer:
    """High-level interface to audio super-resolution models."""

    def __init__(self):
        self._device_info: Optional[DeviceInfo] = None
        self._fastwave = None
        self._audiosr = None
        self._current_mode: Optional[EnhanceMode] = None
        self._target_sr = 48000
        self._num_steps = 4
        self._ddim_steps = 50

    @property
    def device_info(self) -> DeviceInfo:
        if self._device_info is None:
            self._device_info = detect_device()
        return self._device_info

    @property
    def backend(self) -> Backend:
        return self.device_info.backend

    def set_target_sample_rate(self, sr: int):
        self._target_sr = sr

    def set_num_steps(self, steps: int):
        self._num_steps = steps

    def set_ddim_steps(self, steps: int):
        self._ddim_steps = steps

    def load_model(self, mode: EnhanceMode) -> bool:
        """Load the model for the specified mode. Returns True on success."""
        if mode == self._current_mode:
            return True

        self._unload_current()

        try:
            if mode == EnhanceMode.REALTIME:
                return self._load_fastwave()
            else:
                return self._load_audiosr()
        except Exception as e:
            logger.error(f"Failed to load model for {mode.value}: {e}")
            return False

    def _load_fastwave(self) -> bool:
        from src.models.fastwave import FastWaveModel
        self._fastwave = FastWaveModel(self.device_info)
        if not self._fastwave.load():
            self._fastwave = None
            return False
        self._current_mode = EnhanceMode.REALTIME
        logger.info("FastWave model loaded")
        return True

    def _load_audiosr(self) -> bool:
        from src.models.audiosr_model import AudioSRModel
        self._audiosr = AudioSRModel(self.device_info)
        if not self._audiosr.load():
            self._audiosr = None
            return False
        self._current_mode = EnhanceMode.QUALITY
        logger.info("AudioSR model loaded")
        return True

    def _unload_current(self):
        if self._fastwave is not None:
            self._fastwave.unload()
            self._fastwave = None
        if self._audiosr is not None:
            self._audiosr.unload()
            self._audiosr = None
        self._current_mode = None

    def enhance_chunk(self, audio: np.ndarray, input_sr: int) -> np.ndarray:
        """Enhance a single audio chunk (for real-time mode).

        Args:
            audio: float32 array, shape (samples,) or (channels, samples)
            input_sr: source sample rate

        Returns:
            Enhanced float32 array at target sample rate
        """
        if self._fastwave is None:
            raise RuntimeError("FastWave model not loaded")
        return self._fastwave.enhance(audio, input_sr, self._target_sr,
                                      num_steps=self._num_steps)

    def enhance_full(self, audio: np.ndarray, input_sr: int,
                     progress_callback=None) -> np.ndarray:
        """Enhance full audio (for quality/AudioSR mode).

        Args:
            audio: float32 array, shape (samples,) or (channels, samples)
            input_sr: source sample rate
            progress_callback: callable(float) reporting 0.0-1.0 progress

        Returns:
            Enhanced float32 array at target sample rate
        """
        if self._audiosr is None:
            raise RuntimeError("AudioSR model not loaded")
        return self._audiosr.enhance(audio, input_sr, self._target_sr,
                                     progress_callback, ddim_steps=self._ddim_steps)

    def unload(self):
        self._unload_current()

    def is_model_available(self, mode: EnhanceMode) -> bool:
        """Check if model weights are downloaded for the given mode."""
        if mode == EnhanceMode.REALTIME:
            ckpt = MODELS_DIR / "fastwave" / "checkpoint.pth"
            return ckpt.exists()
        else:
            import importlib.util
            return importlib.util.find_spec("audiosr") is not None
