"""Compile source files and write a manifest. Does not import or run the application."""
import hashlib
import json
import py_compile
import sys
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
output = root / "build"
output.mkdir(exist_ok=True)
manifest = {"version": "0.2.0", "kind": "source_compilation_only", "functional_tests": False, "files": {}}
for source_root in (root / "companion", root / "vendor", root / "scripts"):
    for path in sorted(source_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root)
        compiled = output / "bytecode" / relative.with_suffix(".pyc")
        compiled.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(path), cfile=str(compiled), doraise=True)
        manifest["files"][relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
for path in (root / "pyproject.toml", root / "vendor/astrbot/pyproject.toml"):
    tomllib.loads(path.read_text("utf-8"))
for path in (root / "companion/web").iterdir():
    if path.is_file():
        manifest["files"][path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
for path in (root / "desktop").rglob("*"):
    if path.is_file() and not {"bin", "obj"}.intersection(path.relative_to(root).parts):
        manifest["files"][path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
for source_root in (root / "assets", root / "qq-native"):
    for path in source_root.rglob("*"):
        if path.is_file():
            manifest["files"][path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
(output / "source-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
print(f"Compiled {sum(p.endswith('.py') for p in manifest['files'])} Python source files; no app startup or functional tests.")
