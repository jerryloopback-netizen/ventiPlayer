"""Apollo model wrapper: single-pass band-split GAN for lossy-codec music restoration.

Apollo (Look2Hear, ICASSP 2025) repairs music degraded by lossy codecs (MP3/AAC),
reconstructing high-frequency content cut by the codec. Operates at 44.1 kHz,
processes stereo natively, single forward pass (no diffusion) — ~19x realtime.

Source vendored under src/models/apollo_src/. License: CC-BY-SA 4.0.
"""

import logging
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import torch

from src.core.enhancer import Backend, DeviceInfo, MODELS_DIR

logger = logging.getLogger(__name__)

APOLLO_DIR = MODELS_DIR / "apollo"
APOLLO_SR = 44100

# Model hyperparams from config_apollo_uni.yaml (universal lossy enhancer)
_MODEL_ARGS = dict(sr=APOLLO_SR, win=20, feature_dim=384, layer=6)

# Chunked overlap-add (mirrors jarredou's proven inference settings)
_CHUNK_S = 25.0          # seconds per model call
_OVERLAP = 4             # chunk advance = chunk / overlap
_FADE_S = 3.0            # crossfade length in seconds


class ApolloModel:
    """High-level Apollo inference wrapper (stereo, 44.1 kHz, single pass)."""

    def __init__(self, device_info: DeviceInfo):
        self._device_info = device_info
        self._model = None
        self._device = self._resolve_device()

    def _resolve_device(self) -> torch.device:
        if self._device_info.backend == Backend.ROCM:
            return torch.device("cuda")
        return torch.device("cpu")

    def load(self) -> bool:
        ckpt_path = APOLLO_DIR / "apollo_model_uni.ckpt"
        if not ckpt_path.exists():
            logger.error(f"Apollo checkpoint not found at {ckpt_path}")
            return False
        try:
            from src.models.apollo_src import Apollo
            # Build on CPU first, then move to GPU (avoids ROCm LLVM JIT crash)
            self._model = Apollo.from_pretrain(str(ckpt_path), **_MODEL_ARGS)
            self._model = self._model.to(self._device)
            self._model.eval()
            if self._device.type == "cuda":
                self._warmup()
            logger.info(f"Apollo loaded on {self._device}")
            return True
        except Exception as e:
            logger.error(f"Apollo load failed: {e}")
            self._model = None
            return False

    def _warmup(self):
        """Tiny inference pass to force MIOpen/HIP kernel JIT compilation.

        Prevents 'LLVM ERROR: Can't get available size' on first real call.
        """
        try:
            dummy = torch.zeros(1, 2, APOLLO_SR // 2, device=self._device)
            with torch.no_grad():
                self._model(dummy)
            torch.cuda.empty_cache()
            logger.debug("Apollo warmup complete")
        except Exception as e:
            logger.warning(f"Apollo warmup failed (non-fatal): {e}")

    def unload(self):
        if self._model is not None:
            del self._model
            self._model = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    @torch.no_grad()
    def enhance(self, audio: np.ndarray, input_sr: int,
                target_sr: Optional[int] = None,
                progress_callback: Optional[Callable[[float], None]] = None) -> tuple:
        """Restore lossy-codec damage. Apollo does NOT change sample rate.

        Args:
            audio: float32, shape (channels, samples) or (samples,)
            input_sr: source sample rate
            target_sr: ignored (Apollo works at its native 44.1 kHz)
            progress_callback: callable(0..1); raise InterruptedError inside to cancel

        Returns:
            (enhanced float32 shape (channels, samples), output_sr=44100)
        """
        if self._model is None:
            raise RuntimeError("Apollo model not loaded")

        # Normalize to (channels, samples)
        if audio.ndim == 1:
            audio = audio[np.newaxis, :]

        # Resample to Apollo's native 44.1 kHz if needed
        if input_sr != APOLLO_SR:
            audio = self._resample(audio, input_sr, APOLLO_SR)

        if self._device.type == "cuda":
            self._check_vram()

        data = torch.from_numpy(audio).float()  # (nch, T)
        nch, n_samples = data.shape

        chunk = int(_CHUNK_S * APOLLO_SR)
        step = chunk // _OVERLAP
        fade = int(_FADE_S * APOLLO_SR)
        border = chunk - step

        # Reflect-pad edges to reduce boundary artifacts
        if n_samples > 2 * border and border > 0:
            data = torch.nn.functional.pad(data, (border, border), mode="reflect")

        window = self._windowing_array(chunk, fade)
        result = torch.zeros((nch, data.shape[1]), dtype=torch.float32)
        counter = torch.zeros((nch, data.shape[1]), dtype=torch.float32)

        total = data.shape[1]
        i = 0
        while i < total:
            if progress_callback:
                progress_callback(min(0.99, i / total))

            part = data[:, i:i + chunk]
            length = part.shape[-1]
            if length < chunk:
                pad_mode = "reflect" if length > chunk // 2 + 1 else "constant"
                part = torch.nn.functional.pad(part, (0, chunk - length), mode=pad_mode)

            out = self._process_chunk(part)  # (nch, chunk)

            w = window.clone()
            if i == 0:
                w[:fade] = 1.0
            elif i + chunk >= total:
                w[-fade:] = 1.0

            result[:, i:i + length] += out[:, :length] * w[:length]
            counter[:, i:i + length] += w[:length]
            i += step

        counter.clamp_(min=1e-8)
        final = (result / counter).numpy()
        np.nan_to_num(final, copy=False, nan=0.0)

        # Remove the reflect padding
        if n_samples > 2 * border and border > 0:
            final = final[:, border:-border]

        if progress_callback:
            progress_callback(1.0)
        return final.astype(np.float32), APOLLO_SR

    def _process_chunk(self, chunk: torch.Tensor) -> torch.Tensor:
        """Run one (nch, samples) chunk through Apollo. Returns (nch, samples)."""
        x = chunk.unsqueeze(0).to(self._device)  # (1, nch, T)
        out = self._model(x)                      # (1, nch, T)
        result = out.squeeze(0).cpu()
        if self._device.type == "cuda":
            torch.cuda.empty_cache()
        return result

    @staticmethod
    def _windowing_array(window_size: int, fade_size: int) -> torch.Tensor:
        # Linear fade in/out at the chunk edges
        window = torch.ones(window_size)
        window[-fade_size:] *= torch.linspace(1, 0, fade_size)
        window[:fade_size] *= torch.linspace(0, 1, fade_size)
        return window

    @staticmethod
    def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(orig_sr, target_sr)
        up, down = target_sr // g, orig_sr // g
        # resample each channel
        return np.stack([
            resample_poly(ch, up, down).astype(np.float32) for ch in audio
        ])

    def _check_vram(self):
        free_mem = torch.cuda.mem_get_info(0)[0]
        if free_mem < 300 * 1024 * 1024:
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            free_mem = torch.cuda.mem_get_info(0)[0]
            if free_mem < 200 * 1024 * 1024:
                raise RuntimeError(
                    f"VRAM 不足 ({free_mem // (1024 * 1024)}MB)，无法安全执行 Apollo 推理"
                )
