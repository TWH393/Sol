"""Prepare an isolated copy of public QQ program files, never copy account/session data."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(source: Path, destination: Path):
    source, destination = source.resolve(), destination.resolve()
    if not destination.is_relative_to(ROOT / "state/qq") or destination == source:
        raise ValueError("独立客户端必须放在本项目state/qq内")
    versions = []
    for folder in (source / "versions").iterdir():
        manifest = folder / "resources/app/package.json"
        if manifest.is_file():
            data = json.loads(manifest.read_text("utf-8-sig"))
            versions.append((int(data.get("buildVersion", 0)), folder, data))
    if not versions or not (source / "QQ.exe").is_file():
        raise ValueError("未找到可识别的QQ安装程序文件")
    _, version, metadata = max(versions, key=lambda row: row[0])
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.is_file() and entry.suffix.lower() in {".exe", ".dll"} and not entry.name.lower().startswith("uninstall"):
            shutil.copy2(entry, destination / entry.name)
    target_version = destination / "versions" / version.name
    shutil.copytree(version, target_version, dirs_exist_ok=True, ignore=shutil.ignore_patterns("logs", "crashpad", "crashes"))
    for name in ("config.json", "channel.json"):
        if (source / "versions" / name).is_file():
            shutil.copy2(source / "versions" / name, destination / "versions" / name)
    result = {"version": metadata["version"], "source": str(source), "runtime": str(destination),
              "profile": str(destination.parent / "sol-profile"), "third_party_authorization": False,
              "copied_accounts": False, "status": "prepared_not_connected"}
    (destination / "sol-client.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("C:/Program Files/Tencent/QQNT"))
    parser.add_argument("--destination", type=Path, default=ROOT / "state/qq/sol-client")
    args = parser.parse_args()
    prepare(args.source, args.destination)
