"""AudioSR model wrapper: high-quality diffusion-based audio super-resolution.

Uses the `audiosr` pip package (haoheliu/versatile_audio_super_resolution).
Outputs 48kHz. Best for offline/quality mode.
"""

import logging
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import MagicMock

import numpy as np

from src.core.enhancer import Backend, DeviceInfo

logger = logging.getLogger(__name__)


def _patch_torch_distributed():
    """Patch torch.distributed for Windows ROCm where it's incomplete."""
    import torch.distributed as dist
    if not hasattr(dist, 'group'):
        dist.group = MagicMock()
    if not hasattr(dist, 'ReduceOp'):
        dist.ReduceOp = MagicMock()
    if 'torch.distributed.nn' not in sys.modules:
        mock_nn = MagicMock()
        sys.modules['torch.distributed.nn'] = mock_nn
        sys.modules['torch.distributed.nn.functional'] = mock_nn


def _setup_hf_mirror():
    """Patch audiosr's CLAP to load roberta-base from local cache."""
    import os
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

    roberta_local = os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface", "hub",
        "models--roberta-base", "snapshots",
        "0000000000000000000000000000000000000001"
    )
    if not os.path.isdir(roberta_local):
        return

    # Patch transformers to resolve "roberta-base" to local path
    try:
        from transformers import RobertaConfig, RobertaTokenizer
        _orig_config_from = RobertaConfig.from_pretrained
        _orig_tok_from = RobertaTokenizer.from_pretrained

        @classmethod
        def _patched_config(cls, pretrained, *args, **kwargs):
            if pretrained == "roberta-base":
                pretrained = roberta_local
            return _orig_config_from(pretrained, *args, **kwargs)

        @classmethod
        def _patched_tok(cls, pretrained, *args, **kwargs):
            if pretrained == "roberta-base":
                pretrained = roberta_local
            return _orig_tok_from(pretrained, *args, **kwargs)

        RobertaConfig.from_pretrained = _patched_config
        RobertaTokenizer.from_pretrained = _patched_tok
    except Exception:
        pass


def _patch_audiosr_download():
    """Patch audiosr's download_checkpoint to use local file if available."""
    import os
    from pathlib import Path

    hub_dir = Path.home() / ".cache" / "huggingface" / "hub" / "models--haoheliu--audiosr_basic"
    candidates = [
        hub_dir / "pytorch_model.bin",
    ]
    # Also check snapshots subdirectories
    snapshots_dir = hub_dir / "snapshots"
    if snapshots_dir.exists():
        for snap in snapshots_dir.iterdir():
            candidates.append(snap / "pytorch_model.bin")

    local_path = None
    for p in candidates:
        if p.exists():
            local_path = str(p)
            break

    if local_path:
        import audiosr.utils
        import audiosr.pipeline

        def patched_download(checkpoint_name="basic"):
            if checkpoint_name == "basic":
                return local_path
            return audiosr.utils._original_download_checkpoint(checkpoint_name)

        audiosr.utils._original_download_checkpoint = audiosr.utils.download_checkpoint
        audiosr.utils.download_checkpoint = patched_download
        audiosr.pipeline.download_checkpoint = patched_download


def _patch_torchaudio_backend():
    """Fix torchaudio and audiosr lowpass filter issues."""
    try:
        import torchaudio
        import soundfile
        import torch
        import numpy as np

        _original_load = torchaudio.load

        def _sf_load(filepath, *args, **kwargs):
            try:
                return _original_load(filepath, *args, **kwargs)
            except (OSError, ImportError, RuntimeError):
                data, sr = soundfile.read(str(filepath), dtype='float32')
                if data.ndim == 1:
                    data = data[np.newaxis, :]
                else:
                    data = data.T
                return torch.from_numpy(data), sr

        torchaudio.load = _sf_load
    except Exception:
        pass

    # Fix lowpass filter crash when cutoff_freq >= nyquist
    try:
        import audiosr.utils as au
        _orig_lowpass_prep = au.lowpass_filtering_prepare_inference

        def _patched_lowpass_prep(dl_output):
            import numpy as np
            sr = dl_output["sampling_rate"]
            nyq = 0.5 * sr
            # Patch _locate_cutoff_freq result to stay below nyquist
            stft = dl_output["stft"]
            cutoff_freq = (au._locate_cutoff_freq(stft, percentile=0.985) / 1024) * 24000
            if cutoff_freq < 1000:
                cutoff_freq = 24000
            # Clamp to 95% of nyquist to avoid filter instability
            cutoff_freq = min(cutoff_freq, nyq * 0.95)
            dl_output["_patched_cutoff"] = cutoff_freq
            return _orig_lowpass_prep(dl_output)

        # Simpler: just patch the lowpass function to clamp hi
        from audiosr import lowpass as lp_module
        _orig_lowpass_filter = lp_module.lowpass_filter

        def _safe_lowpass_filter(x, highcut, fs, order, ftype):
            nyq = 0.5 * fs
            if highcut >= nyq:
                highcut = nyq * 0.95
            return _orig_lowpass_filter(x, highcut, fs, order, ftype)

        lp_module.lowpass_filter = _safe_lowpass_filter
    except Exception:
        pass


class AudioSRModel:
    """High-level AudioSR inference wrapper using the official pip package."""

    def __init__(self, device_info: DeviceInfo):
        self._device_info = device_info
        self._model = None
        self._device = self._resolve_device()

    def _resolve_device(self) -> str:
        if self._device_info.backend == Backend.ROCM:
            return "cuda"
        return "cpu"

    def load(self) -> bool:
        """Load AudioSR model. Returns True on success."""
        try:
            _patch_torch_distributed()
            _setup_hf_mirror()
            _patch_audiosr_download()
            _patch_torchaudio_backend()
            from audiosr import build_model

            if self._device == "cuda":
                import torch
                # Load on CPU first, then move to GPU to avoid ROCm LLVM JIT crash
                self._model = build_model(model_name="basic", device="cpu")
                self._model = self._model.to("cuda")
                self._model.device = "cuda"
                # Enable memory-efficient attention if available
                torch.cuda.empty_cache()
            else:
                self._model = build_model(model_name="basic", device=self._device)

            logger.info(f"AudioSR loaded on {self._device}")
            return True
        except ImportError as e:
            logger.error(f"audiosr package not available: {e}")
            return False
        except Exception as e:
            logger.error(f"AudioSR load failed: {e}")
            self._model = None
            return False

    def unload(self):
        if self._model is not None:
            del self._model
            self._model = None
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass

    def enhance(self, audio: np.ndarray, input_sr: int, target_sr: int,
                progress_callback: Optional[Callable[[float], None]] = None,
                ddim_steps: int = 50) -> np.ndarray:
        """Enhance full audio using AudioSR diffusion model."""
        if self._model is None:
            raise RuntimeError("AudioSR model not loaded")

        _patch_torch_distributed()
        _patch_torchaudio_backend()
        from audiosr import super_resolution
        import soundfile as sf
        import torch

        if audio.ndim > 1:
            audio = audio.mean(axis=0)

        # Process in segments to fit in VRAM (max ~5s per segment)
        segment_duration = 5.12
        segment_samples = int(segment_duration * input_sr)
        output_sr = 48000
        output_segments = []

        total_segments = max(1, int(np.ceil(len(audio) / segment_samples)))

        for seg_idx in range(total_segments):
            # Check cancel via progress_callback (raises InterruptedError if cancelled)
            if progress_callback:
                progress_callback(seg_idx / total_segments)

            start = seg_idx * segment_samples
            end = min(start + segment_samples, len(audio))
            segment = audio[start:end]

            with tempfile.TemporaryDirectory() as tmp_dir:
                input_path = Path(tmp_dir) / "input.wav"
                sf.write(str(input_path), segment, input_sr, subtype="FLOAT")

                if self._device == "cuda":
                    torch.cuda.empty_cache()
                waveform = super_resolution(
                    self._model,
                    str(input_path),
                    seed=42,
                    guidance_scale=3.5,
                    ddim_steps=ddim_steps,
                    latent_t_per_second=12.8,
                )

            if waveform.ndim == 3:
                result = waveform[0, 0]
            elif waveform.ndim == 2:
                result = waveform[0]
            else:
                result = waveform

            if hasattr(result, "numpy"):
                result = result.float().cpu().numpy()
            else:
                result = np.asarray(result, dtype=np.float32)

            expected_len = int(len(segment) * output_sr / input_sr)
            result = result[:expected_len]
            output_segments.append(result)
            if self._device == "cuda":
                torch.cuda.empty_cache()

        if progress_callback:
            progress_callback(1.0)

        return np.concatenate(output_segments)
