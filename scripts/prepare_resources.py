"""Install media resources without inference or bot login. Safe to resume."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from companion.config import Config

config = Config()
runtime = ROOT / "runtime"
runtime.mkdir(exist_ok=True)
progress_file = config.data / "install-progress.json"


def progress(stage, **fields):
    progress_file.write_text(json.dumps({"stage": stage, **fields}, ensure_ascii=False, indent=2), "utf-8")
    print(stage, flush=True)


def command(*args, env=None):
    result = subprocess.run(args, env=env)
    if result.returncode:
        raise RuntimeError("安装命令失败："+str(args[0]))


def mirror(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        # Hugging Face snapshots may store weight files as links to their blob cache.
        # Copy the resolved bytes rather than preserving a link to the old installation.
        if path.is_symlink() and not path.is_file():
            continue
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir(exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(path.resolve(), target)
            except OSError:
                shutil.copy2(path, target)


def main():
    progress("准备本地助手运行时")
    ollama = runtime / "ollama/ollama.exe"
    if not ollama.is_file():
        installed = ROOT / "toolchain/cache/ollama/v0.40.1"
        if (installed / "ollama.exe").is_file():
            mirror(installed, ollama.parent)
        else:
            archive = runtime / "ollama.zip"
            urllib.request.urlretrieve("https://github.com/ollama/ollama/releases/download/v0.40.1/ollama-windows-amd64.zip", archive)
            with zipfile.ZipFile(archive) as zip_file:
                for member in zip_file.infolist():
                    target = (ollama.parent / member.filename).resolve()
                    if not target.is_relative_to(ollama.parent.resolve()):
                        raise ValueError("运行时归档包含无效路径")
                zip_file.extractall(ollama.parent)
    progress("准备本地辅助模型，不执行推理")
    models = config.data / "resources/ollama"
    cached = ROOT / "toolchain/cache/ollama-models"
    if cached.is_dir():
        mirror(cached, models)
    model_name, _, model_tag = config.get("models", "local_model").partition(":")
    if not (models / "manifests/registry.ollama.ai/library" / model_name / (model_tag or "latest")).is_file():
        from urllib.parse import urlparse
        env = {**os.environ, "OLLAMA_HOST": urlparse(config.get("models", "local_url")).netloc, "OLLAMA_MODELS": str(models)}
        server = subprocess.Popen([str(ollama), "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            import time
            time.sleep(2)
            command(str(ollama), "pull", config.get("models", "local_model"), env=env)
        finally:
            server.terminate()
            server.wait(timeout=20)
    progress("准备本地语音转写模型，不加载模型")
    from huggingface_hub import snapshot_download
    speech = config.data / "resources/whisper-small"
    cached_speech = ROOT / "toolchain/cache/whisper-small"
    if cached_speech.is_dir():
        mirror(cached_speech, speech)
    elif not (speech / "model.bin").is_file():
        snapshot_download("Systran/faster-whisper-small", local_dir=speech)
    config.update({"video": {"whisper_model": str(speech)}})
    progress("准备 FFmpeg")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        for candidate in (runtime / "ffmpeg/bin/ffmpeg.exe",):
            if candidate.is_file():
                ffmpeg = str(candidate)
                break
    if not ffmpeg:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    local_ffmpeg = runtime / "ffmpeg/bin/ffmpeg.exe"
    local_ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    if Path(ffmpeg).resolve() != local_ffmpeg.resolve():
        shutil.copy2(ffmpeg, local_ffmpeg)
    ffmpeg = str(local_ffmpeg)
    config.update({"video": {"ffmpeg": ffmpeg}})
    progress("准备本地图片资源")
    from prepare_images import prepare
    prepare(config, progress)
    progress("依赖与资源准备完成", complete=True, inference_calls=0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        progress("安装未完成", complete=False, error=str(exc))
        raise
