"""Prepare only public QQ program files in Sol's private directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil


def prepare_client(qq_root: Path, source: Path | None = None) -> dict:
    qq_root = qq_root.resolve()
    destination = qq_root / "sol-client"
    if source is None:
        candidates = [Path(os.environ.get(name, "C:/Program Files")) / "Tencent/QQNT"
                      for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")]
        source = next((p for p in candidates if (p / "QQ.exe").is_file()), None)
    if source is None or not (source / "QQ.exe").is_file():
        raise FileNotFoundError("未找到本机官方QQ程序，请先安装Windows版QQ；无需登录个人账号")
    source = source.resolve()
    if source == destination or destination.is_relative_to(source):
        raise ValueError("QQ程序来源不能是Sol独立副本或其父目录")
    config_path = source / "versions/config.json"
    config = json.loads(config_path.read_text("utf-8-sig")) if config_path.is_file() else {}
    versions = []
    for folder in (source / "versions").iterdir():
        manifest = folder / "resources/app/package.json"
        if manifest.is_file():
            info = json.loads(manifest.read_text("utf-8-sig"))
            if info.get("platform") == "win32" and info.get("eleArch") == "x64":
                versions.append((int(info.get("buildVersion", 0)), folder, info))
    if not versions:
        raise ValueError("本机QQ不是当前可接入的Windows x64版本，尚未复制任何账号资料")
    selected = next((v for v in versions if v[1].name == config.get("curVersion")), None) or max(versions, key=lambda v: v[0])
    _, version, metadata = selected
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.is_file() and entry.suffix.lower() in {".exe", ".dll"} and not entry.name.lower().startswith("uninstall"):
            shutil.copy2(entry, destination / entry.name)
    # Copy only the selected public program version, never AppData or user profiles.
    shutil.copytree(version, destination / "versions" / version.name, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("logs", "crashpad", "crashes"))
    config["curVersion"] = version.name
    (destination / "versions/config.json").write_text(json.dumps(config, ensure_ascii=False), "utf-8")
    if (source / "versions/channel.json").is_file():
        shutil.copy2(source / "versions/channel.json", destination / "versions/channel.json")
    result = {"version": metadata["version"], "source": str(source), "runtime": str(destination),
              "profile": str(qq_root / "sol-profile"), "third_party_authorization": False,
              "copied_accounts": False, "status": "prepared_not_connected"}
    # The marker is written last; interrupted copies are never treated as ready.
    (destination / "sol-client.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return result
