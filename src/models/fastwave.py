"""FastWave model: lightweight diffusion audio super-resolution (any -> 48kHz).

Based on Nikait/FastWave. Architecture: EDM preconditioning + NuWave2 backbone
with FFC (Fast Fourier Convolution) and BSFT (Band-wise Spatial Feature Transform).
"""

import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from src.core.enhancer import Backend, DeviceInfo, MODELS_DIR

logger = logging.getLogger(__name__)

FASTWAVE_DIR = MODELS_DIR / "fastwave"
CHECKPOINT_URL = "https://drive.google.com/file/d/1oNCxrKjgiWsYGW6P49rsI84vFYR5G3m8/view?usp=sharing"

TARGET_SR = 48000
CHUNK_SAMPLES = 32768
HOP_LENGTH = 256
FILTER_LENGTH = 1024
WIN_LENGTH = 1024


class GlobalResponseNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.gamma = nn.Parameter(torch.zeros(1, dim, 1))
        self.beta = nn.Parameter(torch.zeros(1, dim, 1))

    def forward(self, x):
        gx = torch.norm(x, p=2, dim=-1, keepdim=True)
        nx = gx / (gx.mean(dim=1, keepdim=True) + 1e-6)
        return self.gamma * (x * nx) + self.beta + x


class DiffusionEmbedding(nn.Module):
    def __init__(self, n_channels=128, scale=50000, out_channels=512):
        super().__init__()
        self.n_channels = n_channels
        self.linear_scale = scale
        self.projection1 = nn.Linear(n_channels, out_channels)
        self.projection2 = nn.Linear(out_channels, out_channels)

    def forward(self, noise_level):
        if len(noise_level.shape) > 1:
            noise_level = noise_level.squeeze(-1)
        half_dim = self.n_channels // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, dtype=torch.float32,
                                      device=noise_level.device) * -emb)
        emb = self.linear_scale * noise_level.unsqueeze(1) * emb.unsqueeze(0)
        emb = torch.cat((emb.sin(), emb.cos()), dim=-1)
        emb = F.silu(self.projection1(emb))
        emb = F.silu(self.projection2(emb))
        return emb


class BSFT(nn.Module):
    def __init__(self, nhidden, out_channels):
        super().__init__()
        self.mlp_shared = nn.Sequential(
            nn.Conv1d(2, 2, kernel_size=3, padding=1, groups=2),
            nn.Conv1d(2, nhidden, kernel_size=1),
        )
        self.mlp_gamma = nn.Conv1d(nhidden, out_channels, kernel_size=1)
        self.mlp_beta = nn.Conv1d(nhidden, out_channels, kernel_size=1)
        self.grn = GlobalResponseNorm(nhidden)

    def forward(self, x, band):
        actv = self.mlp_shared(band)
        actv = self.grn(actv)
        actv = F.silu(actv)
        gamma = self.mlp_gamma(actv).unsqueeze(-1)
        beta = self.mlp_beta(actv).unsqueeze(-1)
        return x * (1 + gamma) + beta


class EfficientFourierUnit(nn.Module):
    def __init__(self, in_channels, out_channels, bsft_channels):
        super().__init__()
        self.register_buffer('hann_window', torch.hann_window(WIN_LENGTH))
        self.conv_layer = nn.Conv2d(in_channels * 2, out_channels * 2,
                                    kernel_size=1, padding=0, bias=False)
        self.bsft = BSFT(bsft_channels, out_channels * 2)

    def forward(self, x, band):
        batch = x.shape[0]
        x_flat = x.view(-1, x.size(-1))

        ffted = torch.stft(x_flat, FILTER_LENGTH, hop_length=HOP_LENGTH,
                           win_length=WIN_LENGTH, window=self.hann_window,
                           center=True, normalized=True, onesided=True,
                           return_complex=True)
        # ffted: (B*C, F, T) complex -> stack real/imag as (B*C, 2, F, T)
        ffted = torch.view_as_real(ffted).permute(0, 3, 1, 2).contiguous()
        # Reshape to (B, C*2, F, T)
        ffted = ffted.view((batch, -1,) + ffted.size()[2:])

        ffted = F.relu(self.bsft(ffted, band))
        ffted = self.conv_layer(ffted)

        # Back to (B*C, 2, F, T) -> (B*C, F, T, 2) -> complex
        ffted = ffted.view((-1, 2,) + ffted.size()[2:]).permute(0, 2, 3, 1).contiguous()
        output = torch.istft(torch.view_as_complex(ffted), FILTER_LENGTH,
                             hop_length=HOP_LENGTH, win_length=WIN_LENGTH,
                             window=self.hann_window, center=True,
                             normalized=True, onesided=True)
        return output.view(batch, -1, x.size(-1))


class SpectralTransform(nn.Module):
    def __init__(self, in_channels, out_channels, bsft_channels):
        super().__init__()
        self.conv1 = nn.Conv1d(in_channels, out_channels // 2, 1, bias=False)
        self.fu = EfficientFourierUnit(out_channels // 2, out_channels // 2,
                                       bsft_channels)
        self.conv2 = nn.Conv1d(out_channels // 2, out_channels, 1, bias=False)

    def forward(self, x, band):
        x = F.silu(self.conv1(x))
        output = self.fu(x, band)
        output = self.conv2(x + output)
        return output


class FFC(nn.Module):
    def __init__(self, in_channels, out_channels, bsft_channels):
        super().__init__()
        in_cg = in_channels // 2
        in_cl = in_channels - in_cg
        out_cg = out_channels // 2
        out_cl = out_channels - out_cg

        self.global_in_num = in_cg

        self.convl2l = nn.Sequential(
            nn.Conv1d(in_cl, in_cl, 3, padding=1, groups=in_cl, bias=False),
            nn.Conv1d(in_cl, out_cl, 1, bias=False),
        )
        self.convl2g = nn.Conv1d(in_cl, out_cg, 1, bias=False)
        self.convg2l = nn.Conv1d(in_cg, out_cl, 1, bias=False)
        self.convg2g = SpectralTransform(in_cg, out_cg, bsft_channels)

    def forward(self, x_l, x_g, band):
        out_xl = self.convl2l(x_l) + self.convg2l(x_g)
        out_xg = self.convl2g(x_l) + self.convg2g(x_g, band)
        return out_xl, out_xg


class ResidualBlock(nn.Module):
    def __init__(self, residual_channels, pos_emb_dim, bsft_channels):
        super().__init__()
        self.ffc1 = FFC(residual_channels, 2 * residual_channels, bsft_channels)
        self.diffusion_projection = nn.Linear(pos_emb_dim, residual_channels)
        self.grn = GlobalResponseNorm(residual_channels)
        self.output_projection = nn.Conv1d(residual_channels,
                                           2 * residual_channels, 1)

    def forward(self, x, band, noise_emb):
        noise_level = self.diffusion_projection(noise_emb).unsqueeze(-1)
        y = x + noise_level

        # Split into local and global
        y_l = y[:, :y.shape[1] - self.ffc1.global_in_num]
        y_g = y[:, y.shape[1] - self.ffc1.global_in_num:]

        y_l, y_g = self.ffc1(y_l, y_g, band)

        # Gate mechanism
        gate_l, filter_l = torch.chunk(y_l, 2, dim=1)
        gate_g, filter_g = torch.chunk(y_g, 2, dim=1)
        gate = torch.cat((gate_l, gate_g), dim=1)
        filt = torch.cat((filter_l, filter_g), dim=1)

        y = torch.sigmoid(gate) * torch.tanh(filt)
        y = self.grn(y)
        y = self.output_projection(y)
        residual, skip = torch.chunk(y, 2, dim=1)
        return (x + residual) / math.sqrt(2.0), skip


class NuWave2(nn.Module):
    def __init__(self, residual_layers=15, residual_channels=64,
                 pos_emb_channels=128, pos_emb_scale=50000,
                 pos_emb_dim=512, bsft_channels=64):
        super().__init__()
        self.input_projection = nn.Conv1d(2, residual_channels, 1)
        self.diffusion_embedding = DiffusionEmbedding(
            pos_emb_channels, pos_emb_scale, pos_emb_dim
        )
        self.residual_layers = nn.ModuleList([
            ResidualBlock(residual_channels, pos_emb_dim, bsft_channels)
            for _ in range(residual_layers)
        ])
        self.len_res = residual_layers
        self.skip_projection = nn.Conv1d(residual_channels,
                                         residual_channels, 1)
        self.output_projection = nn.Conv1d(residual_channels, 1, 1)

    def forward(self, audio, audio_low, band, noise_level):
        x = torch.stack((audio, audio_low), dim=1)
        x = F.silu(self.input_projection(x))
        noise_emb = self.diffusion_embedding(noise_level)
        band_onehot = F.one_hot(band).transpose(1, -1).float()

        skip = 0.0
        for layer in self.residual_layers:
            x, skip_connection = layer(x, band_onehot, noise_emb)
            skip = skip + skip_connection

        x = skip / math.sqrt(self.len_res)
        x = self.skip_projection(x)
        x = F.silu(x)
        return self.output_projection(x).squeeze(1)


class EDMPrecond(nn.Module):
    def __init__(self, sigma_data=0.5, **backbone_kwargs):
        super().__init__()
        self.model = NuWave2(**backbone_kwargs)
        self.sigma_data = sigma_data

    def forward(self, x, sigma, audio_low, band):
        sigma_b = sigma.to(x.dtype)
        c_skip = self.sigma_data ** 2 / (sigma_b ** 2 + self.sigma_data ** 2)
        c_in = 1.0 / torch.sqrt(sigma_b ** 2 + self.sigma_data ** 2)
        c_out = (sigma_b * self.sigma_data /
                 torch.sqrt(sigma_b ** 2 + self.sigma_data ** 2))

        c_skip = c_skip[:, None]
        c_in = c_in[:, None]
        c_out = c_out[:, None]

        c_noise = sigma_b.log() / 4.0
        F_x = self.model(c_in * x, audio_low, band, c_noise)
        return c_skip * x + c_out * F_x


def edm_sampler(net, wav_l, band, num_steps=8, sigma_min=0.002,
                sigma_max=80.0, rho=8.0, device="cpu"):
    """EDM Euler ODE sampler."""
    B, T = wav_l.shape
    step_indices = torch.arange(num_steps, device=device, dtype=torch.float64)
    t_steps = (sigma_max ** (1 / rho) +
               step_indices / (num_steps - 1) *
               (sigma_min ** (1 / rho) - sigma_max ** (1 / rho))) ** rho
    t_steps = torch.cat([t_steps, torch.zeros_like(t_steps[:1])])

    x_next = torch.randn(B, T, device=device) * t_steps[0]

    for i in range(num_steps):
        x_cur = x_next
        t_cur = t_steps[i].float().expand(B)
        t_next_val = t_steps[i + 1].float().expand(B)

        denoised = net(x_cur.float(), t_cur, wav_l, band)
        d_cur = (x_cur.float() - denoised) / t_cur[:, None]
        x_next = x_cur.float() + (t_next_val[:, None] - t_cur[:, None]) * d_cur

    return x_next.clamp(-1.0, 1.0)


class FastWaveModel:
    """High-level FastWave inference wrapper."""

    def __init__(self, device_info: DeviceInfo):
        self._device_info = device_info
        self._model: Optional[EDMPrecond] = None
        self._device = self._resolve_device()
        self._num_steps = 4  # 4 NFE for speed, 8 for quality

    def _resolve_device(self) -> torch.device:
        if self._device_info.backend == Backend.ROCM:
            return torch.device("cuda")
        return torch.device("cpu")

    def load(self) -> bool:
        ckpt_path = FASTWAVE_DIR / "checkpoint.pth"
        if not ckpt_path.exists():
            logger.error(f"Checkpoint not found at {ckpt_path}")
            return False

        try:
            self._model = EDMPrecond(
                sigma_data=0.5,
                residual_layers=15,
                residual_channels=64,
                pos_emb_channels=128,
                pos_emb_scale=50000,
                pos_emb_dim=512,
                bsft_channels=64,
            ).to(self._device)

            ckpt = torch.load(ckpt_path, map_location=self._device,
                              weights_only=False)
            sd = ckpt["state_dict"] if "state_dict" in ckpt else ckpt
            self._model.load_state_dict(sd, strict=False)
            self._model.eval()

            if self._device.type == "cuda":
                self._warmup()

            logger.info(f"FastWave loaded on {self._device}")
            return True
        except Exception as e:
            logger.error(f"FastWave load failed: {e}")
            self._model = None
            return False

    def _warmup(self):
        """Run a tiny inference pass to force ROCm/HIP kernel JIT compilation.

        This prevents 'LLVM ERROR: Can't get available size' crashes that occur
        when kernels are first compiled under memory pressure during real inference.
        """
        try:
            dummy_len = 4096
            dummy_audio = torch.zeros(1, dummy_len, device=self._device)
            n_fft_bins = FILTER_LENGTH // 2 + 1
            dummy_band = torch.zeros(1, n_fft_bins, dtype=torch.long, device=self._device)
            dummy_band[0, :n_fft_bins // 2] = 1
            edm_sampler(self._model, dummy_audio, dummy_band,
                        num_steps=1, device=self._device)
            torch.cuda.empty_cache()
            logger.debug("FastWave warmup complete")
        except Exception as e:
            logger.warning(f"FastWave warmup failed (non-fatal): {e}")

    def unload(self):
        if self._model is not None:
            del self._model
            self._model = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    @torch.no_grad()
    def enhance(self, audio: np.ndarray, input_sr: int,
                target_sr: int) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("Model not loaded")

        if audio.ndim > 1:
            audio = audio.mean(axis=0)

        if self._device.type == "cuda":
            torch.cuda.empty_cache()
            free_mem = torch.cuda.mem_get_info(0)[0]
            # ~200MB minimum for safe inference with chunk overlap
            if free_mem < 200 * 1024 * 1024:
                logger.warning(f"Low VRAM ({free_mem // (1024*1024)}MB free), running GC")
                import gc
                gc.collect()
                torch.cuda.empty_cache()
                free_mem = torch.cuda.mem_get_info(0)[0]
                if free_mem < 100 * 1024 * 1024:
                    raise RuntimeError(
                        f"VRAM 不足 ({free_mem // (1024*1024)}MB)，无法安全执行推理"
                    )

        audio_t = torch.from_numpy(audio).float().to(self._device)

        if input_sr != target_sr:
            target_len = int(len(audio_t) * target_sr / input_sr)
            audio_low = F.interpolate(
                audio_t.unsqueeze(0).unsqueeze(0),
                size=target_len, mode="linear", align_corners=False
            ).squeeze()
        else:
            audio_low = audio_t
            target_len = len(audio_t)

        chunk_size = CHUNK_SAMPLES
        hop = chunk_size // 2
        total_len = len(audio_low)

        n_fft_bins = FILTER_LENGTH // 2 + 1
        cutoff_bin = int(n_fft_bins * input_sr / target_sr)
        band = torch.zeros(1, n_fft_bins, dtype=torch.long,
                           device=self._device)
        band[0, :cutoff_bin] = 1

        # Short audio: single pass without overlap-add windowing
        if total_len <= chunk_size:
            pad_len = chunk_size - total_len
            if pad_len > 0:
                audio_padded = F.pad(audio_low, (0, pad_len))
            else:
                audio_padded = audio_low
            chunk = audio_padded.unsqueeze(0)
            enhanced = edm_sampler(
                self._model, chunk, band,
                num_steps=self._num_steps, device=self._device
            ).squeeze(0)
            output = enhanced[:total_len]
            return output.cpu().numpy()

        pad_len = (chunk_size - total_len % chunk_size) % chunk_size
        if pad_len > 0:
            audio_low = F.pad(audio_low, (0, pad_len))

        padded_len = len(audio_low)
        output = torch.zeros(padded_len, device=self._device)
        window = torch.hann_window(chunk_size, device=self._device)
        norm = torch.zeros(padded_len, device=self._device)

        for start in range(0, padded_len - chunk_size + 1, hop):
            chunk = audio_low[start:start + chunk_size].unsqueeze(0)
            enhanced = edm_sampler(
                self._model, chunk, band,
                num_steps=self._num_steps, device=self._device
            ).squeeze(0)
            output[start:start + chunk_size] += enhanced * window
            norm[start:start + chunk_size] += window

        norm = norm.clamp(min=1e-8)
        output = output / norm
        output = output[:total_len]

        return output.cpu().numpy()
