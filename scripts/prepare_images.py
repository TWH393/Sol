"""Prepare missing local image components; never install into an existing external environment."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    ("diffusion_models", "qwen_image_2.1_int8_convrot.safetensors"),
    ("text_encoders", "qwen3vl_8b_int8_convrot.safetensors"),
    ("vae", "qwen_image_2.1_vae_bf16.safetensors"),
]


def run(*args):
    if subprocess.run(args).returncode:
        raise RuntimeError("图片组件安装失败："+str(args[0]))


def fetch(url, path, md5=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        partial = path.with_suffix(path.suffix+".download")
        urllib.request.urlretrieve(url, partial)
        partial.replace(path)
    if md5 and hashlib.md5(path.read_bytes()).hexdigest() != md5:
        raise ValueError("图片模型校验失败："+path.name)


def prepare(config, progress):
    images = config.get("images")
    old_image, old_comfy = Path(images["local_root"]), Path(images["comfy_root"])
    ready = (old_image / "venv/Scripts/python.exe").is_file() and (old_image / "cutout-venv/Scripts/python.exe").is_file() and (old_comfy / ".venv/Scripts/python.exe").is_file() and (old_comfy / "main.py").is_file()
    ready = ready and all((old_comfy / "models" / folder / name).is_file() for folder, name in REQUIRED)
    ready = ready and all((old_image / "models" / name).is_file() for name in ("rembg/birefnet-general-lite.onnx", "inpaint/big-lama.pt", "Qwen-Image-Edit-2511/model_index.json"))
    if ready:
        progress("已有本地绘图组件已登记；不修改外部环境和模型", image_resources=str(old_image), comfy_resources=str(old_comfy))
        return
    # Missing components go under this application's private runtime, not the old installation.
    home = ROOT / "runtime/images"
    comfy = home / "ComfyUI"
    home.mkdir(parents=True, exist_ok=True)
    if not (comfy / "main.py").is_file():
        progress("准备独立 ComfyUI 0.41.0 源码")
        archive = home / "comfy-source.zip"
        fetch("https://github.com/Comfy-Org/ComfyUI/archive/refs/tags/v0.41.0.zip", archive)
        unpack = home / "source-unpack"
        unpack.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                if not (unpack / member.filename).resolve().is_relative_to(unpack.resolve()):
                    raise ValueError("ComfyUI源码归档包含无效路径")
            source.extractall(unpack)
        found = next(p for p in unpack.iterdir() if (p / "main.py").is_file())
        shutil.copytree(found, comfy, dirs_exist_ok=True)
    image_python = home / "venv/Scripts/python.exe"
    cutout_python = home / "cutout-venv/Scripts/python.exe"
    for python, label in ((image_python,"图片编辑"), (cutout_python,"CPU抠图"), (comfy / ".venv/Scripts/python.exe", "ComfyUI")):
        if not python.is_file():
            progress("创建独立"+label+"环境")
            run(sys.executable, "-m", "venv", str(python.parents[1]))
    for python, lock in ((image_python, "image-edit"), (cutout_python, "image-cutout"), (comfy / ".venv/Scripts/python.exe", "comfy")):
        progress("安装锁定的"+lock+"依赖；不加载模型")
        run(str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements" / (lock+".lock.txt")))
    progress("准备 Qwen 生图与编辑模型；仅下载，不执行推理")
    from huggingface_hub import hf_hub_download, model_info, snapshot_download
    revision = model_info("Comfy-Org/Qwen-Image-2.1").sha
    for folder, name in REQUIRED:
        hf_hub_download("Comfy-Org/Qwen-Image-2.1", filename=folder+"/"+name, revision=revision, local_dir=comfy / "models")
    snapshot_download("Qwen/Qwen-Image-Edit-2511", revision="6f3ccc0b56e431dc6a0c2b2039706d7d26f22cb9", local_dir=home / "models/Qwen-Image-Edit-2511")
    fetch("https://github.com/danielgatis/rembg/releases/download/v0.0.0/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx", home / "models/rembg/birefnet-general-lite.onnx", "4fab47adc4ff364be1713e97b7e66334")
    fetch("https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt", home / "models/inpaint/big-lama.pt", "e3aa4aaa15225a33ec84f9f4bc47e500")
    config.update({"images": {"local_root": str(home), "comfy_root": str(comfy)}})
    progress("独立绘图组件已准备", model_revision=revision)
