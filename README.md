# VentiPlayer

> 面向音乐发烧友的 AI 音频修复流媒体播放器：输入 YouTube / Bilibili 链接，自动取最高码率音轨，经 AI 超分辨率增强后通过 WASAPI Exclusive 直通 DAC，逼近 Hi-Res 听感。

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/Python-3.12-blue)
![Platform](https://img.shields.io/badge/platform-Windows%2011-lightgrey)
![Status](https://img.shields.io/badge/status-MVP-orange)

---

## 目录

- [它能做什么](#它能做什么)
- [目前已经实现的工作](#目前已经实现的工作)
- [架构总览](#架构总览)
- [模块说明](#模块说明)
- [部署指南](#部署指南)
- [运行](#运行)
- [仓库布局与 .gitignore 说明](#仓库布局与-gitignore-说明)
- [已知问题与限制](#已知问题与限制)
- [License](#license)

---

## 它能做什么

- 直接粘贴 **YouTube** / **Bilibili** 链接播放，自动用 yt-dlp 选最高音频码率 + 视频流
- **音频流式 AI 超分辨率**：把任何采样率的源升到 48 kHz
  - **实时模式（FastWave，1.3 M 参数）**：边解码边推理，秒级首播
  - **精听模式（AudioSR ≈ 400 M 参数）**：整段离线处理，输出最高品质
- 增强后的音频经 **mpv WASAPI Exclusive** 输出，绕过 Windows 混音器
- 自带 **A/V 同步管理器**：实时模式下持续监测 audio↔video PTS 漂移，按 30 ms / 80 ms 阈值做软速度修正或硬重定位，失败时无缝 fallback 回原音轨
- B 站 cookie 自动修复 + 大会员状态嗅探（自动选 Hi-Res / 杜比可用音轨）
- 配置 / 缓存 / 模型权重落在 `~/.ventiplayer/`，便于多副本共享

> 这是一份 **MVP 实现**，不是产品。已经能从粘贴 URL 一路走到 DAC 出声，但还有很多边界情况需要打磨。

---

## 目前已经实现的工作

按 `PLAN.md` / `DEBUG_PLAN.md` 中的 Phase 划分：

| Phase | 内容 | 状态 |
|---|---|---|
| 1 — 播放骨架 | PySide6 GUI + mpv 嵌入 + WASAPI Exclusive + yt-dlp | ✅ 完成 |
| 2 — AI 增强 | FastWave 内置实现 + AudioSR pip 包接入 + 双管道 | ✅ 完成 |
| 3 — 同步与稳定 | SyncManager 漂移监测 / seek cooldown / fallback | ✅ 完成 |
| 4 — GUI 完善 | 增强面板 / 状态栏 / 配置持久化 / 快捷键 | ✅ 完成 |

实测目标硬件：

- CPU：任意支持 AVX2 的 x86_64
- GPU：**AMD Radeon RX 9070** (RDNA 4, gfx1200)
- 推理后端：**Windows 原生 PyTorch 2.9 + ROCm 7.2.1**（首选，已验证）
  - DirectML / CPU 作为 fallback（代码已支持，AMD ROCm 不可用时自动选用）
- OS：Windows 11

---

## 架构总览

```
┌──────────────────────────────── PySide6 GUI ────────────────────────────────┐
│  URL 栏 │  视频画面 (mpv 嵌入)  │  进度条 / 音量 / 状态  │  增强面板         │
└───────────────┬─────────────────────────────────────────────────────────────┘
                │
       ┌────────▼────────┐    ┌──────────────────┐
       │ stream.py       │───▶│ StreamInfo       │  yt-dlp 解析 + cookie 修复 +
       │ StreamResolver  │    │ (audio/video URL,│  B 站 nav API VIP 嗅探
       └─────────────────┘    │  http_headers …) │
                              └────────┬─────────┘
                                       │
        ┌──────────────────────────────┼─────────────────────────────────┐
        │                              │                                 │
        ▼                              ▼                                 ▼
┌──────────────────┐        ┌──────────────────────┐         ┌──────────────────────┐
│ player_widget.py │        │ audio_pipe.py        │         │ sync.py              │
│ (mpv 嵌入)       │        │ AudioPipeline        │         │ SyncManager          │
│ video + 原始音轨 │        │ ┌─ realtime_worker ─┐│         │ 后台线程 0.5 s 检漂 │
│                  │◀───────│ │ PyAV 解码 → 5 s   ││────────▶│ 软修正 ±2 % speed   │
│                  │  增强  │ │ chunk → enhancer  ││  switch │ 硬 seek > 80 ms     │
│                  │  WAV   │ │ → 落 enhanced.wav ││  audio  │ fallback 回原音     │
│                  │        │ └───────────────────┘│         └──────────────────────┘
│                  │        │ ┌─ quality_worker ──┐│
│                  │        │ │ 解码全曲 → 一次   ││
│                  │        │ │ AudioSR 推理 →    ││
│                  │        │ │ enhanced.wav      ││
│                  │        │ └───────────────────┘│
└──────────────────┘        └──────────┬───────────┘
                                       │
                                       ▼
                            ┌──────────────────────┐
                            │ enhancer.py          │
                            │ Enhancer (设备检测 + │
                            │ 模式分发)            │
                            └──────┬─────────┬─────┘
                                   │         │
                       ┌───────────▼──┐  ┌───▼──────────────┐
                       │ fastwave.py  │  │ audiosr_model.py │
                       │ EDM+NuWave2  │  │ wraps audiosr pip│
                       │ 内置实现     │  │ + monkey patches │
                       └──────────────┘  └──────────────────┘
```

### 实时模式数据流（核心）

```
URL ──▶ yt-dlp 解析（约 2-3 s）
        │
        ├─▶ 视频/原音轨 URL ──▶ mpv 渲染、原音 WASAPI 出声
        │
        └─▶ 音频 URL ──▶ PyAV 解码 ──▶ 5 s chunk 缓冲
                                          │
                                          ▼
                                FastWave 推理 (~2-4 s/chunk on RX 9070)
                                          │
                                          ▼
                                追加写到 enhanced_realtime.wav (FLOAT)
                                          │
                                          ▼
                            SyncManager 探测 audio ≈ video，时机成熟时
                            switch_audio_fn(enhanced.wav) 替换音轨
                                          │
                                          ▼
                            后台 0.5 s 监测 drift，必要时软修正/硬 seek
```

### 精听模式数据流

```
URL ──▶ 解析 ──▶ PyAV 解码全曲 ──▶ AudioSR 5.12 s 段切分
                                          │
                                          ▼
                                ddim_steps=50 扩散推理 (segment by segment)
                                          │
                                          ▼
                                合并 → enhanced_quality.wav (48 kHz FLOAT)
                                          │
                                          ▼
                                播放器切换到该 WAV
```

---

## 模块说明

### `src/main.py`
QApplication 入口。负责：
- Splash screen + 后台线程预热设备检测（避免主窗口冻结）
- Windows 终端 UTF-8 编码切换、`PATH` 注入 `libmpv` / `deno`
- 顶层异常拦截，避免静默崩溃

### `src/gui/main_window.py`
主窗口。聚合 `MpvPlayerWidget` + `EnhancePanel`，负责：
- URL 解析触发、Stream 信息回填到 UI
- 把 `Enhancer` / `AudioPipeline` / `SyncManager` 串起来
- `PipelineState` 状态机回调到 GUI（进度、错误提示、自动 fallback 提示）
- 快捷键：空格暂停 / ←→ seek / ↑↓ 音量 / Ctrl+L 粘贴 URL

### `src/gui/player_widget.py`
mpv 嵌入组件。
- 通过 `python-mpv` + `wid=widget.winId()` 把视频画到 Qt 窗口
- 用 `QTimer` 周期轮询 `time-pos` / `audio-pts` / `duration`，**不**走 mpv 的事件回调（避免 PySide6 跨线程信号问题）

### `src/gui/enhance_panel.py`
增强面板。模式切换、采样率选择、num_steps / ddim_steps 设置。内置一个带「已增强长度」标记的进度条，让用户直观看到流式增强追到哪一秒。

### `src/core/stream.py`
yt-dlp Python API 封装：
- `fix_cookie_file()`：Netscape cookie 文件中 `domain ` 与 `domain_flag` 不一致会让 Python 3.14 cookiejar 拒绝加载，写一份临时修复版到 `tempdir/ventiplayer/`
- `check_cookie_status()` → 调 `https://api.bilibili.com/x/web-interface/nav` 返回 VIP 状态、用户名
- `StreamResolver.resolve()` 同步 / `resolve_async()` 后台线程；cookie 失败时自动 retry 一次裸请求并把 `cookie_failed=True` 上报给 UI

### `src/core/audio_pipe.py`
两个 worker 线程（**daemon**，cancel 时 `.join(timeout=0.5)` 不等卡死的推理）：
- `_realtime_worker`：PyAV demux → 5 s chunk → `enhancer.enhance_chunk()` → `soundfile.SoundFile.write+flush` 实时追加；遇 VRAM 不足报 recoverable 错误
- `_quality_worker`：解全曲 → `enhancer.enhance_full()` → 一次写出
- 启动时 `_cleanup_stale_temp_dirs()` 回收上次崩溃留下的 `tempdir/ventiplayer_*`

### `src/core/enhancer.py`
- `detect_device()`：torch.cuda → onnxruntime DML → CPU 三路探测
- 懒加载 `FastWaveModel` / `AudioSRModel`，切换模式时旧模型 `unload()` + `torch.cuda.empty_cache()`
- `is_model_available()` 给 GUI 提示「模型未下载」

### `src/core/sync.py`
`SyncManager`：
- `MAX_DRIFT_MS = 80`、`SOFT_DRIFT_MS = 30`、`SYNC_CHECK_INTERVAL = 0.5`
- `_check_drift()` 在 `seek_cooldown=1.0` 秒内不触发，避免 seek 抖动误判
- 软修正 ±2 % speed，硬 resync 直接对增强音轨 seek 到 video pos
- `fallback_to_original(reason)` 在任何失败路径上把音轨换回 mpv 自带的原音轨

### `src/models/fastwave.py`
**单文件 PyTorch 内置实现**（不依赖外部 pip 包）：
- 架构：EDM precond + NuWave2 backbone（FFC + BSFT + GRN）
- 推理：EDM Euler ODE，4 步（默认）或 8 步
- 32 768 样本 chunk + Hann 窗 50 % overlap-add 防边界 click
- ROCm 首次 JIT 在显存压力下会触发 `LLVM ERROR: Can't get available size` 崩溃，因此 `load()` 后跑一次 1024 样本 dummy 推理 **预热**
- 推理前嗅探剩余 VRAM，< 100 MB 直接抛 `VRAM 不足`（让上层 fallback）

### `src/models/audiosr_model.py`
直接调用 `audiosr` pip 包，但加了一堆 monkey patch 才能在 Windows ROCm 跑通：
- `_patch_torch_distributed`：Windows 上 `torch.distributed` 不全，mock 掉 `group` / `ReduceOp` / `torch.distributed.nn`
- `_setup_hf_mirror`：把 `roberta-base`（CLAP 依赖）从 `hf-mirror.com` 拉到本地，然后 patch `RobertaConfig.from_pretrained` 改用本地路径
- `_patch_audiosr_download`：让 audiosr 的 `download_checkpoint` 读 `~/.cache/huggingface/hub/models--haoheliu--audiosr_basic/.../pytorch_model.bin`
- `_patch_torchaudio_backend`：torchaudio 在某些路径下报错，fallback 到 soundfile；同时 patch `lowpass_filter` 把 `cutoff >= nyquist` 的不稳定情况夹到 `0.95 * nyq`
- 推理：5.12 s 段切分 → `super_resolution(...)` ddim_steps=50 → 拼接

### `src/config/settings.py`
JSON 落盘 + 1 秒防抖 timer，避免拖滚动条/调音量时频繁写文件。退出时 `flush()` 强制落盘。

---

## 部署指南

> 这是一个 **MVP**，仓库故意不打包模型权重和原生运行时（加起来超过 1.7 GB）。
> 部署流程是：clone 仓库 → 建虚拟环境 → 装依赖 → 运行 `download_models.py` → 手动放置 `libmpv-2.dll` 和 `deno.exe` → 启动。

### 0. 前置条件

- **OS**：Windows 11 (x64)
- **Python**：3.12（项目中 venv 命名 `.venv312` 暗示这一点）
- **GPU（推荐）**：AMD Radeon RX 7000/9000 系列 + Adrenalin Edition 26.1.1+ 驱动；ROCm 7.2.1 Windows 原生 wheel
  - 没有合适 GPU 时会自动 fallback 到 DirectML 或 CPU；CPU 模式下 FastWave 实时跟不上、AudioSR 接近不可用
- **磁盘**：≈ 2 GB（venv ≈ 13 GB 另算）

### 1. 克隆仓库 & 建虚拟环境

```powershell
git clone <你的远端 URL> VentiPlayer
cd VentiPlayer

py -3.12 -m venv .venv312
.\.venv312\Scripts\activate
python -m pip install -U pip
```

### 2. 安装 PyTorch

ROCm 路线（推荐，AMD 用户）：

```powershell
# 参考 AMD 官方页：https://rocm.docs.amd.com/projects/install-on-windows/
# 形如：
pip install torch==2.9 --index-url https://repo.radeon.com/rocm/manylinux/...
```

> ROCm Windows wheel 的具体 index-url 会随版本更新，请去 AMD 文档拿最新链接。

CUDA / CPU 路线：照 https://pytorch.org/get-started/locally/ 选对应命令。

### 3. 安装其他依赖

```powershell
pip install -r requirements.txt
pip install gdown          # download_models.py 需要
pip install onnxruntime    # 可选：让设备检测能识别 DirectML
```

### 4. 下载模型权重

```powershell
python download_models.py
```

这会下载 3 份资产：

| 模型 | 大小 | 落盘位置 |
|---|---|---|
| FastWave checkpoint | ≈ 16 MB | `~/.ventiplayer/models/fastwave/fastwave_checkpoint.pt` |
| AudioSR `pytorch_model.bin` | ≈ 1.6 GB | `~/.cache/huggingface/hub/models--haoheliu--audiosr_basic/...` |
| `roberta-base` 词表 | ≈ 5 MB | `~/.cache/huggingface/hub/models--roberta-base/...` |

> ⚠ **已知 bug**：`download_models.py` 把 FastWave 写为 `fastwave_checkpoint.pt`，但 `enhancer.py` / `fastwave.py` 期望 `checkpoint.pth`。下载完后手动 rename：
>
> ```powershell
> cd $HOME\.ventiplayer\models\fastwave
> Rename-Item fastwave_checkpoint.pt checkpoint.pth
> ```
>
> （这是开发期遗留差异，PLAN.md 已记录。后续可以选择修一致。）

### 5. 放置原生运行时

仓库不上传以下两个二进制（`.gitignore` 会拦），需要自己放到仓库根目录：

| 文件 | 大小 | 用途 | 来源 |
|---|---|---|---|
| `libmpv-2.dll` | ≈ 114 MB | mpv 核心，提供视频解码 + WASAPI Exclusive 输出 | https://mpv.io/installation/ → "libmpv build for Windows" |
| `deno.exe` | ≈ 123 MB | yt-dlp 在 YouTube JS 挑战时调用 | https://deno.com/ → 官方 Windows 下载 |

放好后目录应该长这样：

```
VentiPlayer/
├── libmpv-2.dll      ← 你刚放的
├── deno.exe          ← 你刚放的
├── run.py
├── start.bat
├── src/...
└── ...
```

`run.py` 会自动把仓库根目录加进 `PATH`，所以 mpv 和 deno 在这就能被找到。

### 6. （可选）准备 cookies

需要 Bilibili 大会员 Hi-Res 音轨 / 登录态时：

- 浏览器导出 Netscape 格式 cookies.txt（Edge / Chrome / Firefox 都行）
- 默认放 `cookies/cookies_all.txt`，或在 GUI 设置里改
- 程序会自动 `fix_cookie_file()` 修复格式问题再喂给 yt-dlp

> `cookies/` 目录已在 `.gitignore` 中，不会上传。

---

## 运行

```powershell
.\start.bat
```

或者手动：

```powershell
.\.venv312\Scripts\python.exe run.py
```

`start.bat` 多做了几件事，建议用它：

- 设 `MIOPEN_USER_DB_PATH` / `MIOPEN_CUSTOM_CACHE_DIR` 到 `C:\temp\miopen_cache`（避免 ROCm MIOpen 在用户目录写中文路径炸）
- 设 `HF_ENDPOINT=https://hf-mirror.com` 让 huggingface 走国内镜像
- 设 `PYTORCH_ALLOC_CONF=expandable_segments:True` 缓解显存碎片

启动后：

1. 主窗口出现，状态栏显示检测到的推理后端（如 `ROCm: AMD Radeon RX 9070`）
2. 粘贴 URL 到顶部输入框，回车
3. 默认走原音轨先播；想增强就在右侧面板勾选模式 → 等待 enhanced.wav 写够 → 自动切换音轨

---

## 仓库布局与 .gitignore 说明

```
VentiPlayer/
├── run.py                # 入口
├── start.bat             # Windows 启动脚本
├── download_models.py    # 一次性下载所有模型
├── requirements.txt
├── PLAN.md               # 设计计划（已与实现对齐）
├── DEBUG_PLAN.md         # 调试历程（已清理完毕，归档）
├── README.md             # 当前文件
├── LICENSE               # MIT
├── src/
│   ├── main.py
│   ├── gui/{main_window,player_widget,enhance_panel}.py
│   ├── core/{stream,audio_pipe,enhancer,sync}.py
│   ├── models/{fastwave,audiosr_model}.py
│   └── config/settings.py
│
├── models/.gitkeep       # 占位（实际权重在 ~/.ventiplayer）
│
├── .gitignore
├── .gitattributes
├── .vscode/settings.json # 仅保留 interpreter 路径
│
│ —————— 以下都不入库 ——————
├── .venv312/             ≈ 13 GB
├── libmpv-2.dll          ≈ 114 MB
├── deno.exe              ≈ 123 MB
└── cookies/              用户登录态
```

### `.gitignore` 取舍逻辑

需要平衡两件事：**仓库要小** vs **别人能照着部署**。我的取舍是：

| 类别 | 处理方式 | 理由 |
|---|---|---|
| `.venv312/` | 完全忽略 | 13 GB，且 venv 不可移植，必须各自重建 |
| `libmpv-2.dll` / `deno.exe` | 忽略二进制本体，README 留下载链接 | 总计 240 MB，且 mpv 有官方分发；redistribute 也要遵守 LGPL，让用户自己装更干净 |
| 模型权重（`*.pth` / `*.bin` / `*.safetensors`） | 忽略 + 提供 `download_models.py` | AudioSR 单文件就 1.6 GB，git 里塞会让 clone 永远不结束 |
| `~/.ventiplayer/` / `~/.cache/` | 忽略（也确实不在 repo 里） | 用户态运行时，多副本共享 |
| `cookies/` | 忽略 | 含登录 token，**绝对不能进 git** |
| `*.wav` / `*.mp4` / `*.log` 等 | 忽略 | 测试时容易顺手扔进仓库 |
| `.vscode/` | 仅保留 `settings.json` | 别人 clone 后 IDE 自动指向 `.venv312`，不需要其他 launch/profiles |
| `models/.gitkeep` | 入库一个空文件 | 让目录结构与代码里的引用对齐，但不传任何权重 |

### 别人 clone 后，部署"补全"流程

仓库里 **不会有** 的东西，`download_models.py` 和「部署指南」都讲清楚了怎么补：

1. **venv** → `py -3.12 -m venv .venv312` + `pip install`
2. **PyTorch** → 按 GPU 类型选官方 wheel
3. **模型权重** → `python download_models.py`（FastWave 走 Google Drive，需 `gdown`；AudioSR / roberta 走 hf-mirror）
4. **`libmpv-2.dll`** → mpv 官网下载
5. **`deno.exe`** → deno 官网下载
6. **cookies**（可选） → 浏览器导出，按需放入 `cookies/`

---

## 已知问题与限制

- **FastWave checkpoint 文件名不一致**（见上面的 rename 说明）。后续可以把 `download_models.py:84` 的 `fastwave_checkpoint.pt` 改成 `checkpoint.pth`，或者反过来调整 `enhancer.py` / `fastwave.py`，二者择一。
- **首次 ROCm 推理可能崩** (`LLVM ERROR: Can't get available size`)：FastWave 已经在 `load()` 里做 1024 样本 warm-up 规避；AudioSR 较难规避，建议先用小段音频试一次。
- **AudioSR 显存峰值高**：5.12 s 段切分仍可能在 RX 9070（16 GB）上吃满。可以下调 `latent_t_per_second` 或减小段长。
- **B 站非大会员**：能播 1080P 但只有 192 kbps AAC，AI 增强对低码率源效果不如 Hi-Res 源明显。
- **YouTube 反爬**：yt-dlp 偶尔需要 deno + cookies 才能解析；解析失败会在 GUI 提示并打日志。
- **Linux / macOS 未测**：依赖中的 ROCm Windows wheel + libmpv-2.dll 都是 Windows 专属；理论上 PyAV / yt-dlp 跨平台可用，但 SyncManager 的 mpv 嵌入和音频后端没有在其他平台验证过。
- **多语言支持**：UI 文案、日志混合中英文。

---

## License

MIT — see [LICENSE](LICENSE).

第三方组件保留各自许可：

- mpv / libmpv：GPLv2+ / LGPLv2.1+
- yt-dlp：Unlicense
- AudioSR (haoheliu/versatile_audio_super_resolution)：Apache 2.0
- FastWave 实现参考 Nikait/FastWave，权重原作者保留版权
- PySide6：LGPLv3 / GPLv2 / 商业三可选

如果你打算 redistribute 二进制构建，请自行核对 mpv / Qt 的 LGPL 合规性。
