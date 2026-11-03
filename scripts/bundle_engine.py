"""Bundle a private CPython and installed dependencies, never a relocatable venv.

Copies only this project's source and public runtime assets. Models/dependency
files can share immutable disk bytes with the development cache through hard
links; source, manifests and settings are always independent files.
No application imports, inference, service startup or user settings migration.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"__pycache__", ".git", ".cache", "bin", "obj"}


def copy_tree(source, destination, *, immutable=False, exclude=(), omit_pdf_layout=False):
    count = 0
    for folder, directories, names in os.walk(source):
        directories[:] = [d for d in directories if d not in SKIP and d not in exclude]
        relative = Path(folder).relative_to(source)
        if omit_pdf_layout and relative == Path("."):
            directories[:] = [d for d in directories if not (d.startswith("pymupdf_layout-") and d.endswith(".dist-info"))]
        if omit_pdf_layout and relative == Path("pymupdf"):
            # Core rendering does not require the optional layout/ONNX/NumPy
            # extension. PyMuPDF imports it automatically if present.
            directories[:] = [d for d in directories if d != "layout"]
        target = destination / relative
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            if name.endswith((".pyc", ".pyo", ".egg-link")) or name == "direct_url.json" or name in exclude or name.startswith(("__editable__", "_editable_impl_")):
                continue
            source_file, dest = Path(folder) / name, target / name
            if source_file.is_symlink() and not source_file.is_file():
                continue
            # Never put source-site .pth absolute paths into the private interpreter.
            if name.endswith(".pth") and name != "pywin32.pth":
                continue
            if dest.exists():
                dest.unlink()
            if immutable:
                try:
                    os.link(source_file.resolve(), dest)
                except OSError:
                    shutil.copy2(source_file, dest)
            else:
                shutil.copy2(source_file, dest)
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--resources", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "build"):
        raise ValueError("构建目录必须位于本项目 build 内")
    # This script is run by the project's prepared venv, whose base is a full
    # CPython distribution. Only interpreter files and stdlib are read from it.
    base = Path(sys.base_prefix)
    site = Path(sys.prefix) / "Lib/site-packages"
    if not (base / "python.exe").is_file() or not site.is_dir() or sys.prefix == sys.base_prefix:
        raise RuntimeError("请使用本项目 .venv 中的 Python 构建私有运行时")
    python = output / "python"
    python.mkdir(parents=True, exist_ok=True)
    for pattern in ("python*.exe", "python*.dll", "vcruntime*.dll", "LICENSE.txt"):
        for path in base.glob(pattern):
            shutil.copy2(path, python / path.name)
    count = copy_tree(base / "Lib", python / "Lib", immutable=True, exclude={"site-packages"})
    for name in ("DLLs", "tcl"):
        if (base / name).is_dir():
            count += copy_tree(base / name, python / name, immutable=True)
    count += copy_tree(site, python / "Lib/site-packages", immutable=True, omit_pdf_layout=True)
    # Existing output directories may still contain an earlier optional plugin.
    # Move it outside the distribution; leave shared source environments intact.
    target_site = python / "Lib/site-packages"
    optional = [target_site / "pymupdf/layout", *target_site.glob("pymupdf_layout-*.dist-info")]
    for item in optional:
        if not item.exists():
            continue
        if item.is_symlink() or not item.resolve().is_relative_to(python.resolve()):
            raise ValueError("可选PDF扩展路径未隔离，拒绝移动")
        import datetime
        backup = ROOT / "build" / ("pdf-layout-backup-"+datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        if not backup.resolve().is_relative_to((ROOT / "build").resolve()):
            raise ValueError("可选扩展备份目录无效")
        backup.mkdir()
        item.rename(backup / item.name)
    engine = output / "engine"
    for name in ("companion", "vendor", "assets"):
        count += copy_tree(ROOT / name, engine / name)
    for name in ("THIRD_PARTY.md", "LICENSE", "pyproject.toml"):
        shutil.copy2(ROOT / name, engine / name)
    # Never copy state/settings.json, vault.key, DBs, profiles or credentials.
    if (ROOT / "runtime/qq_native").is_dir():
        count += copy_tree(ROOT / "runtime/qq_native", engine / "runtime/qq_native")
    resources = False
    if args.resources:
        for name in ("ollama", "whisper-small"):
            source = ROOT / "state/resources" / name
            if not source.is_dir():
                raise FileNotFoundError("缺少已准备资源：" + str(source))
            count += copy_tree(source, output / "resources" / name, immutable=True)
        count += copy_tree(ROOT / "runtime/ollama", engine / "runtime/ollama", immutable=True)
        # Browser revisions are determined by the installed Playwright package.
        browsers_file = site / "playwright/driver/package/browsers.json"
        browsers = json.loads(browsers_file.read_text("utf-8"))["browsers"]
        cache = ROOT / "runtime/browsers"
        for browser in browsers:
            if browser["name"] not in {"chromium", "ffmpeg", "winldd"}:
                continue
            revision = browser.get("revisionOverrides", {}).get("win64", browser["revision"])
            name = browser["name"].replace("-", "_") + "-" + revision
            if not (cache / name).is_dir():
                raise FileNotFoundError("缺少网页截图组件：" + name)
            count += copy_tree(cache / name, output / "browsers" / name, immutable=True)
        resources = True
    receipt = {"product": "Sol", "version": "0.2.0", "python": sys.version.split()[0], "files": count,
               "private_python": True, "prepared_resources": resources, "credentials_included": False,
               "app_started": False, "functional_tests": False}
    (output / "runtime-manifest.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), "utf-8")
    print(f"Sol private runtime bundled: {count} files; application not started.")


if __name__ == "__main__":
    main()
