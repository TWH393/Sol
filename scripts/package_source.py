"""Source delivery only; never includes runtime data, weights or credentials."""
import datetime
import json
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
allowed = {"companion", "scripts", "vendor", "docs", "desktop", "assets", "qq-native", "requirements"}
single = {"Sol.sln", "global.json", ".python-version", "pyproject.toml", "requirements.lock.txt", "README.md", "THIRD_PARTY.md", "LICENSE", "CHANGELOG.md", "AGENTS.md", "安装Sol.cmd", "启动Sol.cmd", "更新Sol.cmd", ".gitignore"}
exclude = {"__pycache__", ".git", "bin", "obj", "node_modules"}
output = ROOT / "dist"
output.mkdir(exist_ok=True)
destination = output / ("Sol-source-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
    for name in sorted(single):
        if (ROOT / name).is_file():
            archive.write(ROOT / name, name)
    for name in sorted(allowed):
        for folder, directories, names in os.walk(ROOT / name):
            directories[:] = [d for d in directories if d not in exclude]
            for leaf in sorted(names):
                path = Path(folder) / leaf
                if path.suffix not in {".pyc", ".pyo", ".user"}:
                    archive.write(path, path.relative_to(ROOT).as_posix())
receipt = {"product": "Sol", "version": "0.2.0", "source_archive": str(destination), "bytes": destination.stat().st_size,
           "native_app": str(ROOT / "build/Sol/Sol.exe"), "app_started_by_packaging": False, "functional_tests": False}
(ROOT / "build/delivery.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False), "utf-8")
print(destination)
