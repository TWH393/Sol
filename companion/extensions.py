"""Extension installation, source records and explicit compatibility levels."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

from .storage import uid


class Extensions:
    def __init__(self, app):
        self.app = app
        self.lock = asyncio.Lock()
        self.root = app.store.root / "extensions"
        self.root.mkdir(exist_ok=True)

    def classify(self, root):
        if not (root / "metadata.yaml").is_file() and any(".git" not in p.parts for p in root.rglob("SKILL.md")):
            return "skill", ["技能脚本只在任务Docker执行，不加载到Sol主进程"]
        files = list(root.rglob("*.py"))
        text = "\n".join(p.read_text("utf-8", errors="replace") for p in files if ".git" not in p.parts)
        hooks = ("monkeypatch", "setattr(", "sys.modules", "session_lock_manager", "provider_manager.", "get_using_provider(", "get_provider_by_id(", ".text_chat(", "tool_loop_agent(", ".request_llm(", ".send =", "_query_stream", "os.system(", "ctypes.windll", "cron_manager", "asyncio.create_task(")
        reasons = [s for s in hooks if s in text]
        if reasons:
            return "needs_adaptation", reasons
        if (root / "companion.json").is_file():
            return "basic", ["声明支持统一服务，尚待运行验收"]
        if (root / "metadata.yaml").is_file() and files:
            return "basic", []
        if list(root.rglob("SKILL.md")):
            return "skill", []
        return "needs_adaptation", ["没有识别到插件入口或技能说明"]

    async def install(self, source, actor="@robot", kind="auto", subpath=""):
        if actor == "@robot" and not self.app.config.get("permissions", "install_extensions"):
            raise PermissionError("自主安装扩展权限已关闭")
        parsed = urlparse(source)
        if parsed.scheme != "https" or parsed.hostname not in {"github.com", "gitlab.com", "gitee.com"} or parsed.username or parsed.password:
            raise ValueError("请提供 GitHub/GitLab/Gitee 的 HTTPS 仓库链接")
        ref = ""
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname == "github.com" and len(parts) >= 5 and parts[2] in {"tree", "blob"}:
            ref = parts[3]
            selected_path = "/".join(parts[4:])
            if parts[2] == "blob":
                selected_path = selected_path.rsplit("/", 1)[0] if "/" in selected_path else ""
            subpath = subpath or selected_path
            source = "https://github.com/"+"/".join(parts[:2])
        if not shutil.which("git"):
            raise RuntimeError("Git 组件未安装")
        ident = "ext_"+hashlib.sha256((source+subpath+ref).encode()).hexdigest()[:16]
        if self.app.store.one("SELECT id FROM extensions WHERE id=?", (ident,)):
            row = self.app.store.one("SELECT compatibility,enabled FROM extensions WHERE id=?", (ident,))
            return {"id": ident, "compatibility": row["compatibility"], "enabled": bool(row["enabled"]), "reused": True}
        async with self.lock:
            target = self.root / ident
            if target.exists():
                quarantine = self.root / (ident+"-incomplete-"+str(time.time_ns()))
                target.rename(quarantine)
            environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
            process = await asyncio.create_subprocess_exec("git", "clone", "--depth", "1", *(["--branch", ref] if ref else []), "--", source, str(target), env=environment, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), 180)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if process.returncode:
                raise RuntimeError("扩展下载失败："+stderr.decode(errors="replace")[-500:])
            plugin = (target / subpath).resolve() if subpath else target
            if not plugin.is_relative_to(target.resolve()) or not plugin.is_dir():
                raise ValueError("扩展子目录无效")
            level, reasons = self.classify(plugin)
            manifest = {"root": str(plugin), "actor": actor, "reasons": reasons, "source": source, "subpath": subpath, "ref": ref}
            requirements = plugin / "requirements.txt"
            if requirements.exists():
                manifest["requirements"] = requirements.read_text("utf-8", errors="replace")
                manifest["dependency_status"] = "pending_dependency_review"
            enabled = level == "skill"
            if level in {"integrated", "basic"} and self.app.astrbot.context is not None and not requirements.exists():
                folder = self.app.store.root / "astrbot/data/plugins" / ident
                shutil.copytree(plugin, folder, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
                if not requirements.exists():
                    try:
                        await self.app.astrbot.reload_plugin(ident)
                        enabled = True
                    except Exception as exc:
                        reasons.append("插件加载失败："+str(exc))
                        level = "needs_adaptation"
            now = time.time()
            self.app.store.insert("extensions", {"id": ident, "name": plugin.name, "source": source, "kind": "skill" if level == "skill" else "plugin", "compatibility": level, "enabled": int(enabled), "manifest": json.dumps(manifest, ensure_ascii=False), "error": "；".join(reasons) if reasons else None, "created": now, "updated": now})
            if level == "needs_adaptation":
                self.app.store.insert("reports", {"id": uid("r_"), "bot": self.app.config.get("account", "id"), "text": "扩展需要适配，保持停用："+ident+"\n"+"；".join(reasons), "created": now})
            self.app.store.event("extension", "扩展已安装" if enabled else "扩展已下载，等待适配或依赖处理", {"id": ident, "level": level, "source": source})
            return {"id": ident, "compatibility": level, "enabled": enabled, "reasons": reasons, "dependencies_pending": requirements.exists()}

    async def dependencies(self, ident, allow_updates=False):
        row = self.app.store.one("SELECT * FROM extensions WHERE id=?", (ident,))
        if not row:
            raise ValueError("扩展不存在")
        manifest = json.loads(row["manifest"])
        requirements = Path(manifest["root"]) / "requirements.txt"
        if row["kind"] == "skill":
            return {"changed": False, "backend": "docker", "message": "技能依赖在执行任务的Docker内自主安装，不写入Sol主进程环境"}
        if not requirements.exists():
            return {"changed": False}
        # Resolve before installation so an added extension cannot silently upgrade existing packages.
        import sys
        report = self.app.store.root / "jobs" / (ident+"-dependencies.json")
        report.parent.mkdir(exist_ok=True)
        command = [sys.executable, "-m", "pip", "install", "--dry-run", "--report", str(report), "-r", str(requirements)]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
        _, err = await process.communicate()
        if process.returncode:
            raise RuntimeError("依赖解析失败："+err.decode(errors="replace")[-800:])
        from importlib.metadata import version, PackageNotFoundError
        plan = json.loads(report.read_text("utf-8"))
        upgrades = []
        for package in plan.get("install", []):
            name, wanted = package["metadata"]["name"], package["metadata"]["version"]
            try:
                current = version(name)
                if current != wanted:
                    upgrades.append({"name": name, "from": current, "to": wanted})
            except PackageNotFoundError:
                pass
        if upgrades and not allow_updates:
            manifest["dependency_status"] = "admin_update_required"
            self.app.store.execute("UPDATE extensions SET manifest=?,updated=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), time.time(), ident))
            return {"needs_admin": True, "upgrades": upgrades}
        command = [sys.executable, "-m", "pip", "install", "-r", str(requirements)]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
        _, err = await process.communicate()
        if process.returncode:
            raise RuntimeError("依赖安装失败："+err.decode(errors="replace")[-800:])
        manifest["dependency_status"] = "installed"
        level, reasons = self.classify(Path(manifest["root"]))
        self.app.store.execute("UPDATE extensions SET manifest=?,compatibility=?,error=?,updated=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), level, "；".join(reasons) or None, time.time(), ident))
        if level != "needs_adaptation" and self.app.astrbot.context is not None:
            await self.toggle(ident, True)
        return {"changed": True, "upgrades": upgrades}

    async def toggle(self, ident, enabled):
        row = self.app.store.one("SELECT * FROM extensions WHERE id=?", (ident,))
        if not row:
            raise ValueError("扩展不存在")
        if row["compatibility"] == "needs_adaptation" and enabled:
            raise ValueError("此插件需适配，不能当作已兼容启用")
        if row["kind"] != "skill":
            if self.app.astrbot.context is None:
                raise ValueError("AstrBot 核心尚未启动")
            manager = self.app.astrbot.context._star_manager
            manifest = json.loads(row["manifest"])
            if enabled and manifest.get("requirements") and manifest.get("dependency_status") != "installed":
                raise ValueError("请先处理插件依赖，不能绕过已有组件升级确认")
            if enabled:
                root = Path(manifest["root"]).resolve()
                if not root.is_relative_to(self.root.resolve()):
                    raise ValueError("插件目录超出管理范围")
                folder = self.app.store.root / "astrbot/data/plugins" / ident
                if not folder.exists():
                    shutil.copytree(root, folder, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
                try:
                    await self.app.astrbot.reload_plugin(ident)
                except Exception as exc:
                    self.app.store.execute("UPDATE extensions SET enabled=0,compatibility='needs_adaptation',error=?,updated=? WHERE id=?", ("加载失败："+str(exc)[:600], time.time(), ident))
                    raise
            metadata = next((p for p in self.app.astrbot.context.get_all_stars() if p.root_dir_name == ident), None)
            if metadata:
                await (manager.turn_on_plugin(metadata.name) if enabled else manager.turn_off_plugin(metadata.name))
        self.app.store.execute("UPDATE extensions SET enabled=?,updated=? WHERE id=?", (int(enabled), time.time(), ident))
        self.app.astrbot.sync_external_tools()

    def _row(self, ident):
        row = self.app.store.one("SELECT * FROM extensions WHERE id=?", (ident,))
        if not row:
            raise ValueError("扩展不存在")
        return row, json.loads(row["manifest"])

    def _idle(self):
        if self.app.store.one("SELECT COUNT(*) AS n FROM tasks WHERE status IN ('running','queued')")["n"]:
            raise ValueError("当前还有执行或排队任务，请先完成或停止，再更新/卸载扩展")

    def configuration(self, ident, values=None):
        row, manifest = self._row(ident)
        if row["kind"] == "skill":
            return {"schema": {}, "values": {}, "message": "技能直接按说明使用，没有独立配置表"}
        root = Path(manifest["root"])
        schema_path = root / "_conf_schema.json"
        schema = json.loads(schema_path.read_text("utf-8")) if schema_path.exists() else {}
        if values is None:
            metadata = next((p for p in self.app.astrbot.context.get_all_stars() if p.root_dir_name == ident), None) if self.app.astrbot.context else None
            return {"schema": schema, "values": dict(metadata.config) if metadata and metadata.config is not None else manifest.get("configuration", {})}
        if not isinstance(values, dict):
            raise ValueError("插件配置需要JSON对象")
        if set(values)-set(schema):
            raise ValueError("配置包含未声明的字段")
        manifest["configuration"] = values
        self.app.store.execute("UPDATE extensions SET manifest=?,updated=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), time.time(), ident))
        folder = self.app.store.root / "astrbot/data/config"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (ident+"_config.json")).write_text(json.dumps(values, ensure_ascii=False, indent=2), "utf-8")
        return {"saved": True}

    async def update(self, ident):
        self._idle()
        row, manifest = self._row(ident)
        async with self.lock:
            root = Path(manifest["root"]).resolve()
            repository = root
            while repository != self.root.resolve() and not (repository / ".git").is_dir():
                repository = repository.parent
            if repository == self.root.resolve() or not repository.is_relative_to(self.root.resolve()):
                raise ValueError("扩展仓库路径无效")
            backup = self.root / (ident+"-version-"+str(time.time_ns()))
            await asyncio.to_thread(shutil.copytree, repository, backup)
            process = await asyncio.create_subprocess_exec("git", "-C", str(repository), "pull", "--ff-only", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
            try:
                out, err = await asyncio.wait_for(process.communicate(), 180)
                if process.returncode:
                    raise RuntimeError("扩展更新失败："+err.decode(errors="replace")[-600:])
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if row["enabled"]:
                await self.toggle(ident, False)
            deployed = self.app.store.root / "astrbot/data/plugins" / ident
            if deployed.is_dir():
                archive = self.root / (ident+"-deployed-"+str(time.time_ns()))
                deployed.rename(archive)
            level, reasons = self.classify(root)
            manifest.update(previous_root=str(backup), reasons=reasons, update_log=out.decode(errors="replace")[-2000:])
            requirement = root / "requirements.txt"
            if requirement.exists():
                manifest.update(requirements=requirement.read_text("utf-8"), dependency_status="pending_dependency_review")
            self.app.store.execute("UPDATE extensions SET compatibility=?,enabled=0,manifest=?,error=?,updated=? WHERE id=?", (level, json.dumps(manifest, ensure_ascii=False), "；".join(reasons) or None, time.time(), ident))
            if not requirement.exists() and level != "needs_adaptation":
                await self.toggle(ident, True)
            self.app.store.event("extension", "管理员更新了扩展", {"id": ident, "backup": str(backup)})
            return {"updated": True, "compatibility": level, "dependencies_pending": requirement.exists(), "previous_version_kept": True}

    async def uninstall(self, ident):
        self._idle()
        row, manifest = self._row(ident)
        async with self.lock:
            if row["enabled"]:
                await self.toggle(ident, False)
            deployed = self.app.store.root / "astrbot/data/plugins" / ident
            if deployed.is_dir():
                deployed.rename(self.root / (ident+"-removed-"+str(time.time_ns())))
            self.app.store.execute("DELETE FROM extensions WHERE id=?", (ident,))
            self.app.astrbot.sync_external_tools()
            self.app.store.event("extension", "管理员卸载了扩展，源码保留在扩展归档中", {"id": ident})
            return {"removed": True, "source_preserved": True}

    async def rollback(self, ident):
        self._idle()
        row, manifest = self._row(ident)
        previous = Path(manifest.get("previous_root", "")).resolve()
        if not previous.is_relative_to(self.root.resolve()) or not previous.is_dir() or previous == self.root.resolve():
            raise ValueError("没有可恢复的旧扩展源码")
        if row["enabled"]:
            await self.toggle(ident, False)
        old_root = Path(manifest["root"]).resolve()
        relative = Path(manifest.get("subpath", ""))
        manifest["root"] = str(previous / relative)
        manifest.pop("previous_root", None)
        level, reasons = self.classify(previous / relative)
        deployed = self.app.store.root / "astrbot/data/plugins" / ident
        if deployed.exists():
            deployed.rename(self.root / (ident+"-rollback-"+str(time.time_ns())))
        self.app.store.execute("UPDATE extensions SET manifest=?,compatibility=?,enabled=0,error=?,updated=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), level, "；".join(reasons) or None, time.time(), ident))
        self.app.store.event("extension", "扩展源码已恢复，依赖版本不会自动降级", {"id": ident, "from": str(old_root), "to": manifest["root"]})
        return {"restored": True, "requires_enable": True}

    def skills(self):
        result = []
        for row in self.app.store.rows("SELECT * FROM extensions WHERE kind='skill' AND enabled=1"):
            root = Path(json.loads(row["manifest"])["root"])
            for path in root.rglob("SKILL.md"):
                if ".git" not in path.parts:
                    result.append({"id": row["id"], "name": path.parent.name, "path": str(path), "preview": path.read_text("utf-8", errors="replace")[:300]})
        return result

    def skill(self, name):
        matches = [s for s in self.skills() if name in {s["name"], s["id"]+"::"+s["name"]}]
        if len(matches) != 1:
            raise ValueError("技能不存在或名称不唯一；先read_skill列出完整名称")
        skill = matches[0]
        root = Path(skill["path"]).resolve().parent
        if not root.is_relative_to(self.root.resolve()):
            raise PermissionError("技能目录不属于Sol管理的扩展")
        return skill, root

    async def run_skill(self, message, task_id, name, script, arguments=None, file_ids=None, finalize=False):
        if not self.app.config.get("features", "python"):
            raise PermissionError("Docker执行功能已关闭")
        skill, root = self.skill(name)
        entry = (root / script).resolve()
        if not entry.is_relative_to(root) or not entry.is_file() or entry.suffix not in {".py", ".sh", ".js", ".mjs"}:
            raise ValueError("脚本须为技能目录内的Python/Shell/JavaScript文件")
        import io
        import zipfile
        import shlex
        arguments = arguments or []
        if any(not isinstance(a, str) or "\x00" in a for a in arguments):
            raise ValueError("技能参数必须为文本列表")
        def pack():
            buffer = io.BytesIO()
            total = 0
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in root.rglob("*"):
                    relative = path.relative_to(root)
                    if path.is_symlink() or any(p in {".git", ".venv", "node_modules", "__pycache__"} or p.startswith(".env") for p in relative.parts) or not path.is_file():
                        continue
                    if not path.resolve().is_relative_to(root):
                        continue
                    total += path.stat().st_size
                    if total > 20*1024*1024:
                        raise ValueError("技能资源超过20MB，请只保留执行需要的脚本和模板")
                    archive.writestr(relative.as_posix(), path.read_bytes())
            return buffer.getvalue()
        payload = await asyncio.to_thread(pack)
        digest = hashlib.sha256(payload).hexdigest()[:16]
        dest = "/work/skills/"+digest
        # Stage via the same bounded file channel rather than embedding large archives in code.
        bundle = self.app.files.add(payload, "skill-"+digest+".zip", message, task_id, "material", False, {"extension": skill["id"]})
        stage = "import pathlib,zipfile\np=pathlib.Path("+repr(dest)+");p.mkdir(parents=True,exist_ok=True)\nwith zipfile.ZipFile('input/skill-"+digest+".zip') as z: z.extractall(p)\nfor n in ('input','output'):\n if not (p/n).exists(): (p/n).symlink_to('/work/'+n,target_is_directory=True)\nprint('技能资源已准备')"
        await self.app.python.execute(message, task_id, stage, [bundle]+(file_ids or []))
        requirements = ""
        if (root / "requirements.txt").exists():
            requirements += "python -m pip install --user -r "+shlex.quote(dest+"/requirements.txt")+" && "
        if (root / "package.json").exists():
            requirements += "npm install --prefix "+shlex.quote(dest)+" --ignore-scripts && "
        runner = {".py": "python", ".sh": "bash", ".js": "node", ".mjs": "node"}[entry.suffix]
        stamp = shlex.quote(dest+"/.sol-deps-ready")
        command = "cd "+shlex.quote(dest)+" && "+("(test -f "+stamp+" || { "+requirements+"touch "+stamp+"; }) && " if requirements else "")+runner+" "+shlex.quote(entry.relative_to(root).as_posix())+" "+" ".join(shlex.quote(a) for a in arguments)
        result = await self.app.python.shell(message, task_id, command, file_ids or [], finalize)
        result.data.update(skill=name, script=script, backend="docker", skill_root=dest)
        return result
