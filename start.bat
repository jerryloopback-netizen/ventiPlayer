@echo off
cd /d "%~dp0"
set MIOPEN_USER_DB_PATH=C:\temp\miopen_cache
set MIOPEN_CUSTOM_CACHE_DIR=C:\temp\miopen_cache
set HF_ENDPOINT=https://hf-mirror.com
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
set PYTORCH_ALLOC_CONF=expandable_segments:True
if not exist "C:\temp\miopen_cache" mkdir "C:\temp\miopen_cache"
.venv312\Scripts\python.exe run.py
pause
