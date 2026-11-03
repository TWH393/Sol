"""One disposable Docker environment per task, shared by Python, Shell and Skills."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from ..domain import ToolResult
from ..storage import uid
from . import STRING, STRINGS


class PythonSessions:
    def __init__(self, app):
        self.app = app
        self.sessions = {}
        self.locks = {}
        self.build_lock = asyncio.Lock()
        self.image = None
        self.build_error = ""

    async def run_command(self, *args, input=None, timeout=120):
        docker = shutil.which("docker")
        if not docker:
            raise RuntimeError("缺少 Docker Desktop；请安装并启动后再执行，模型代码不会改在主机运行")
        process = await asyncio.create_subprocess_exec(docker, *args, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(input), timeout)
            if process.returncode:
                reason = stderr.decode("utf-8", errors="replace")[-2400:] or ("容器内存不足或进程被终止" if process.returncode == 137 else "操作未完成")
                raise RuntimeError(f"Docker {args[0] if args else ''} 失败（退出码{process.returncode}）："+reason)
            return stdout
        except TimeoutError as exc:
            raise TimeoutError("Docker 操作超时："+str(args[0] if args else "unknown")) from exc
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def prepare(self):
        async with self.build_lock:
            if self.image:
                return self.image
            root = Path(__file__).resolve().parents[1] / "workers"
            digest = hashlib.sha256(b"".join((root / p).read_bytes() for p in ("Dockerfile", "session.py", "client.py"))).hexdigest()[:12]
            tag = "sol-tools:"+digest
            try:
                await self.run_command("image", "inspect", tag, timeout=20)
            except RuntimeError:
                log = self.app.store.root / "jobs" / "docker-build.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                self.app.store.event("docker", "正在准备常用工具镜像，首次需要下载依赖", {"image": tag})
                try:
                    output = await self.run_command("build", "--progress=plain", "-t", tag, str(root), timeout=1800)
                    log.write_bytes(output)
                except Exception as exc:
                    self.build_error = str(exc)
                    log.write_text(str(exc), "utf-8")
                    raise RuntimeError("Docker 常用工具镜像准备失败："+str(exc)) from exc
            self.image, self.build_error = tag, ""
            return tag

    async def _session(self, message, task_id):
        if task_id in self.sessions:
            return self.sessions[task_id]
        self.app.tasks.progress(task_id, "准备 Docker 任务环境与常用软件包")
        image = await self.prepare()
        owner = self.app.tasks.get(task_id)["owner"]
        scope = hashlib.sha256((message.bot+":"+message.scope+":"+owner).encode()).hexdigest()[:20]
        name = "sol-task-"+task_id.replace("_", "-")
        token = uid("owner_")
        session = {"name": name, "token": token, "touched": time.time(), "owner": owner, "scope": message.scope, "bot": message.bot}
        self.app.store.checkpoint("docker-session:"+task_id, session)
        self.sessions[task_id] = session
        # No host directory, host sockets, credentials, or privileged mode are mounted.
        # The writable root is disposable. Dependency caches are scoped to the caller.
        try:
            await self.run_command("run", "-d", "--name", name, "--label", "sol.task="+task_id, "--label", "sol.owner="+token,
                "--network", "bridge", "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
                "--cap-add", "FOWNER", "--cap-add", "SETUID", "--cap-add", "SETGID", "--security-opt", "no-new-privileges",
                "--pids-limit", "160", "--memory", "3g", "--cpus", "2", "--tmpfs", "/work:rw,nosuid,nodev,size=1g,mode=1777",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777", "--mount", "type=volume,source=sol-deps-"+scope+",target=/cache",
                "--env", "PIP_CACHE_DIR=/cache/pip", "--env", "npm_config_cache=/cache/npm", image)
            await self.run_command("exec", "-u", "0", name, "sh", "-c", "mkdir -p /cache/pip /cache/npm; chown -R 10001:10001 /cache", timeout=30)
            await self.run_command("exec", name, "python", "-c", "import time, pathlib; p=pathlib.Path('/tmp/companion.sock');\nfor i in range(100):\n if p.exists(): break\n time.sleep(.1)\nelse: raise RuntimeError('task session socket not ready')", timeout=15)
        except BaseException:
            await self.release(task_id)
            raise
        return session

    async def request(self, message, task_id, payload, file_ids=None, finalize=False):
        if not self.app.config.get("features", "python"):
            raise PermissionError("Docker执行功能已关闭")
        lock = self.locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            session = await self._session(message, task_id)
            task = self.app.tasks.get(task_id)
            if session["bot"] != message.bot or session["scope"] != message.scope or session["owner"] != task["owner"]:
                raise PermissionError("Docker 环境不属于本次任务")
            files, names = [], set()
            inputs = session.setdefault("inputs", {})
            for ident in file_ids or []:
                row, path = self.app.files.resolve(ident, message)
                name = row["name"] if row["name"] not in names and inputs.get(row["name"], row["hash"]) == row["hash"] else ident+"-"+row["name"]
                names.add(name)
                inputs[name] = row["hash"]
                files.append({"id": ident, "name": name, "data": base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()})
            code = payload.get("code") or payload.get("command", "")
            if len(code) > 200000:
                raise ValueError("代码或命令过大，请分步骤运行")
            extension = ".sh" if payload.get("mode") == "shell" else ".py"
            code_id = self.app.files.add(code.encode(), "执行代码-"+str(time.time_ns())+extension, message, task_id, "code", False, {"input_files": file_ids or [], "backend": "docker"})
            payload.update(files=files, timeout=min(600, max(1, int((task["deadline"] or time.time()+180)-time.time()))))
            try:
                raw = await self.run_command("exec", "-i", session["name"], "python", "/opt/client.py", input=json.dumps(payload).encode(), timeout=payload["timeout"]+15)
            except BaseException:
                await self.release(task_id)
                raise
            result = json.loads(raw)
            session["touched"] = time.time()
            ids = []
            for item in result.get("files", []):
                ident = self.app.files.add(base64.b64decode(item["data"], validate=True), item["name"], message, task_id, "output", False,
                    {"generated_by": payload.get("mode", "python"), "backend": "docker", "code_file": code_id, "input_files": file_ids or []})
                self.app.files.reference(code_id, "file", ident)
                ids.append(ident)
            if not result.get("ok"):
                error = result.get("error", "执行失败")
                if result.get("missing_module"):
                    error += "\n缺少软件包："+result["missing_module"]+"；可调用 install_packages 安装，再只修复失败步骤，不能盲目重跑成功部分"
                raise RuntimeError(error)
            return ToolResult(result.get("stdout") or "执行完成", ids, {"backend": "docker", "inputs": [{"file_id": i, "path": "input/"+f["name"]} for i, f in zip(file_ids or [], files)], "direct_text": bool((result.get("stdout") or "").strip())}, bool(finalize))

    async def execute(self, message, task_id, code, file_ids=None, finalize=False):
        return await self.request(message, task_id, {"mode": "python", "code": code}, file_ids, finalize)

    async def shell(self, message, task_id, command, file_ids=None, finalize=False):
        return await self.request(message, task_id, {"mode": "shell", "command": command}, file_ids, finalize)

    async def install(self, message, task_id, packages, manager="pip"):
        if not self.app.config.get("permissions", "install_extensions"):
            raise PermissionError("自主安装依赖权限已关闭")
        if manager not in {"pip", "apt", "npm"} or not packages or len(packages) > 40:
            raise ValueError("包管理器须为 pip/apt/npm，单次填写1至40个包名")
        pattern = r"[A-Za-z0-9][A-Za-z0-9_.\[\],<>=!~+/-]*" if manager == "pip" else r"@?[A-Za-z0-9][A-Za-z0-9_./@=+~-]*"
        if any(not isinstance(p, str) or not re.fullmatch(pattern, p) or "://" in p or ".." in p or p.startswith("/") for p in packages):
            raise ValueError("请填写软件包名称和版本，不能把命令或本机路径填进包名")
        import shlex
        quoted = " ".join(shlex.quote(p) for p in packages)
        if manager == "apt":
            lock = self.locks.setdefault(task_id, asyncio.Lock())
            async with lock:
                session = await self._session(message, task_id)
                raw = await self.run_command("exec", "-u", "0", "-e", "DEBIAN_FRONTEND=noninteractive", session["name"], "sh", "-c", "apt-get update && apt-get install -y --no-install-recommends "+quoted, timeout=600)
                session["touched"] = time.time()
            return ToolResult(raw.decode(errors="replace")[-12000:], data={"backend": "docker", "packages": packages, "manager": manager})
        command = "python -m pip install --user "+quoted if manager == "pip" else "npm install --prefix /work/.node "+quoted
        result = await self.shell(message, task_id, command)
        result.data.update(packages=packages, manager=manager)
        return result

    async def release(self, task_id):
        session = self.sessions.get(task_id)
        if session:
            try:
                raw = await self.run_command("inspect", "--format", "{{json .Config.Labels}}", session["name"], timeout=15)
                labels = json.loads(raw)
                if labels.get("sol.owner") != session.get("token") or labels.get("sol.task") != task_id:
                    raise PermissionError("容器所有权标签不匹配，未结束其他容器")
                await self.run_command("rm", "-f", session["name"], timeout=20)
            except Exception as exc:
                if "No such" in str(exc):
                    self.sessions.pop(task_id, None)
                    self.locks.pop(task_id, None)
                    self.app.store.execute("DELETE FROM checkpoints WHERE key=?", ("docker-session:"+task_id,))
                    return
                self.app.store.event("docker", "任务容器清理未确认，保留记录等待后续回收", {"task_id": task_id, "container": session["name"], "error": str(exc)}, "warning")
                return
            self.sessions.pop(task_id, None)
            self.app.store.execute("DELETE FROM checkpoints WHERE key=?", ("docker-session:"+task_id,))
        self.locks.pop(task_id, None)

    async def cleanup(self):
        for row in self.app.store.rows("SELECT key,value FROM checkpoints WHERE key LIKE 'docker-session:%'"):
            ident = row["key"].split(":", 1)[1]
            if ident not in self.sessions:
                session = json.loads(row["value"])
                if session.get("name") == "sol-task-"+ident.replace("_", "-") and session.get("token"):
                    self.sessions[ident] = session
        for ident, session in list(self.sessions.items()):
            if self.locks.get(ident) and self.locks[ident].locked():
                continue
            row = self.app.store.one("SELECT status FROM tasks WHERE id=?", (ident,))
            if not row or row["status"] not in {"queued", "running"} or time.time()-session["touched"] > self.app.config.get("files", "python_idle_seconds"):
                await self.release(ident)

    def register(self, registry):
        common = {"file_ids": STRINGS, "finalize": {"type": "boolean"}}
        registry.add("run_python", "code", "任务专属Docker执行Python，保留Python变量，与Shell/技能共用文件。input/为输入，output/为产物；同名输入通过input-manifest.json按文件ID定位。已装科学计算、Office/PDF、图表、OCR和中文字体。可自行install_packages补依赖。直接交付finalize=true；排版用真实字体。", {"code": STRING, **common}, ["code"], self.execute, "python")
        registry.add("run_shell", "code", "在同任务Docker执行Shell；可用FFmpeg、LibreOffice、Pandoc、压缩、下载等。与Python共用/work，产物放output/。不在Windows主机执行。", {"command": STRING, **common}, ["command"], self.shell, "python")
        registry.add("install_packages", "code", "自主安装任务需要的pip/apt/npm依赖，只改Docker，下载缓存按使用者隔离。安装后只重做失败步骤，不重跑成功部分。", {"packages": STRINGS, "manager": {"type": "string", "enum": ["pip", "apt", "npm"]}}, ["packages"], self.install, "python")
