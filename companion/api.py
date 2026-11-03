"""Local-only API and shared desktop/browser assets. No manual chat-send route."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

import httpx
import psutil
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .application import Application
from .config import ROOT
from .diagnostics import scrub


class WebAssets(StaticFiles):
    """Versioned module directories keep every relative ES import on the same build."""
    async def get_response(self, path, scope):
        parts = path.replace("\\", "/").split("/", 1)
        if len(parts) == 2 and parts[0].startswith("r-"):
            path = parts[1]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


def create_api(application=None):
    service = application or Application()
    session = secrets.token_urlsafe(40)
    desktop_token = os.environ.get("SOL_DESKTOP_TOKEN", "")
    web_root = ROOT / "companion/web"
    web_revision = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(web_root.iterdir()) if p.is_file())).hexdigest()[:16]

    @asynccontextmanager
    async def lifespan(api):
        service.background_task(service.start(), "application-startup")
        yield
        await service.stop()

    api = FastAPI(title="Sol", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    api.state.service = service

    @api.middleware("http")
    async def local_access(request: Request, next_handler):
        port = service.config.get("app", "port")
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = request.headers.get("origin")
        if request.client.host not in {"127.0.0.1", "::1"} or request.headers.get("host") not in hosts or origin and origin not in {"http://"+h for h in hosts} or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "仅允许本机应用访问"}, 403)
        desktop_access = bool(desktop_token) and secrets.compare_digest(request.headers.get("x-sol-desktop", ""), desktop_token)
        if request.url.path.startswith("/api/") and not desktop_access and not secrets.compare_digest(request.cookies.get("companion_session", ""), session):
            return JSONResponse({"error": "请先打开应用页面"}, 401)
        try:
            return await next_handler(request)
        except (ValueError, PermissionError, FileNotFoundError) as exc:
            return JSONResponse({"error": str(exc)}, 400)
        except Exception as exc:
            service.store.event("api", "操作失败", {"path": request.url.path, "error": str(exc)}, "error")
            return JSONResponse({"error": str(exc)[:600]}, 500)

    @api.get("/")
    async def index():
        markup = (web_root / "index.html").read_text("utf-8").replace("/assets/", f"/assets/r-{web_revision}/")
        response = HTMLResponse(markup, headers={"Cache-Control": "no-store"})
        response.set_cookie("companion_session", session, httponly=True, samesite="strict")
        return response

    api.mount("/assets", WebAssets(directory=web_root), name="assets")
    api.mount("/brand", StaticFiles(directory=ROOT / "assets/branding"), name="branding")

    @api.get("/api/desktop/health")
    async def desktop_health():
        return {"product": "Sol", "protocol": 1, "pid": os.getpid(), "data": str(service.store.root), "stopping": service.stopping}

    @api.get("/api/desktop/startup")
    async def desktop_startup():
        # Readiness is a connection fact, never inferred from an animation timer
        # or a successful HTTP bind. This route does not launch or test anything.
        if service.stopping:
            return {"phase": "stopping", "stage": "正在停止内部服务"}
        error = service.startup_error or service.astrbot.error
        if error:
            return {"phase": "error", "stage": "内部服务启动未完成", "error": error}
        if service.astrbot.context is None:
            return {"phase": "initializing", "stage": service.startup_stage}
        account = service.config.get("account")
        if not account["enabled"] or not account.get("auto_login", True):
            return {"phase": "ready", "stage": "管理界面已就绪", "manual_login": True}
        if service.online:
            return {"phase": "ready", "stage": "QQ 已连接"}
        if service.qq_startup_error:
            return {"phase": "error", "stage": "QQ 启动未完成", "error": service.qq_startup_error}
        qq = await service.services.qq_status()
        phase = qq.get("phase", "connecting")
        if qq.get("error") and phase in {"error", "offline"}:
            return {"phase": "error", "stage": "QQ 连接未完成", "error": qq["error"]}
        needs_scan = phase in {"needs_scan", "waiting_qrcode", "waiting_scan", "waiting_confirmation", "qrcode_failed"}
        stages = {"stopped": "正在启动 QQ", "initializing": "正在准备 QQ", "restoring_login": "正在恢复已保存的登录", "starting_session": "正在准备消息连接", "ready": "正在连接机器人"}
        return {"phase": "needs_scan" if needs_scan else "connecting", "stage": "请在 Sol 内扫码登录" if needs_scan else stages.get(phase, "正在连接 QQ"), "error": qq.get("error", "")}

    @api.post("/api/desktop/shutdown")
    async def desktop_shutdown(request: Request):
        if not desktop_token or not secrets.compare_digest(request.headers.get("x-sol-desktop", ""), desktop_token):
            return JSONResponse({"error": "仅桌面主程序可以停止内部引擎"}, 403)
        shutdown = getattr(api.state, "request_shutdown", None)
        if shutdown is None:
            raise ValueError("当前不是 Sol 受管引擎")
        shutdown()
        return {"stopping": True}

    @api.get("/api/state")
    async def state():
        memory = psutil.virtual_memory()
        cfg = service.config.snapshot()
        task_counts = {r["status"]: r["n"] for r in service.store.rows("SELECT status,COUNT(*) AS n FROM tasks GROUP BY status")}
        usage = service.store.rows("SELECT model,source,COUNT(*) AS calls,SUM(input) AS input,SUM(output) AS output,SUM(CASE WHEN input IS NULL OR output IS NULL THEN 1 ELSE 0 END) AS unknown,SUM(cost) AS cost,AVG(duration) AS duration FROM usage WHERE created>? GROUP BY model,source", (time.time()-86400,))
        progress = service.store.root / "install-progress.json"
        install = json.loads(progress.read_text("utf-8")) if progress.is_file() else {"complete": False, "stage": "尚无安装完成记录"}
        return {"web_revision": web_revision, "online": service.online, "uptime": time.time()-service.started, "name": cfg["app"]["name"], "theme": cfg["app"].get("theme", "light"), "account": cfg["account"], "model": cfg["models"]["selected"], "installation": install, "core": {"running": service.astrbot.context is not None, "error": service.astrbot.error}, "local": {"calls": service.local.calls, "ready": service.local.calls > 0 and not service.local.last_error, "error": service.local.last_error}, "resources": {"cpu": psutil.cpu_percent(), "memory_used": memory.used, "memory_total": memory.total, "process_memory": psutil.Process().memory_info().rss, "disk_free": shutil.disk_usage(service.store.root).free, "active": service.tasks.resources.active}, "task_counts": task_counts, "open_faults": service.store.one("SELECT COUNT(*) AS n FROM faults WHERE status='open'")["n"], "usage": usage, "public_usage": service.models.usage("@public"), "events": service.store.rows("SELECT * FROM events ORDER BY created DESC LIMIT 30"), "persona": service.personality.current(), "file_count": service.store.one("SELECT COUNT(*) AS n,SUM(size) AS bytes FROM files WHERE status='ready'"), "memory_count": service.store.one("SELECT COUNT(*) AS n FROM memories WHERE active=1")["n"], "pending_reports": service.store.one("SELECT COUNT(*) AS n FROM reports WHERE delivered=0")["n"]}

    @api.get("/api/settings")
    async def settings():
        from .reasoning import effective
        value = service.config.snapshot()
        value["models"]["providers"] = [effective(p) for p in value["models"]["providers"]]
        from .native_capabilities import capabilities, effective_capabilities
        for provider in value["models"]["providers"]:
            provider["native_capabilities"] = capabilities(provider, value["native"])
        selected = next((p for p in value["models"]["providers"] if p["id"] == value["models"]["selected"]), {})
        value["effective_capabilities"] = effective_capabilities(service, selected)
        value["native_skills"] = [{"id": s["id"]+"::"+s["name"], "name": s["name"]} for s in service.extensions.skills()]
        return value

    @api.post("/api/native/skills/upload")
    async def upload_native_skill(request: Request):
        payload = await request.json()
        return await service.native.publish_skill(payload.get("extension_id", ""))

    @api.post("/api/tools/docker/prepare")
    async def prepare_docker_tools():
        if not service.python.build_lock.locked() and not any(t.get_name() == "prepare-docker-tools" and not t.done() for t in service.background):
            service.background_task(service.python.prepare(), "prepare-docker-tools")
        return {"status": "preparing", "message": "常用工具镜像在后台准备，结果写入活动记录；不会运行模型代码"}

    @api.post("/api/settings")
    async def update_settings(request: Request):
        payload = await request.json()
        ui_only = not (set(payload)-{"layout", "app"}) and not (set(payload.get("app", {})) - {"theme"})
        if not ui_only:
            await service.backups.create("before-settings", False)
        previous = service.config.snapshot()
        result = service.config.update(payload)
        service.store.event("config", "管理员更新了设置", {"sections": list(payload)})
        if "models" in payload:
            await service.astrbot.sync_models()
        if "features" in payload and "plugins" in payload["features"]:
            await service.astrbot.apply_extension_switch()
        if "account" in payload:
            result["restart_required"] = previous["account"] != result["account"]
        return result

    @api.post("/api/secrets")
    async def set_secret(request: Request):
        payload = await request.json()
        name = payload.get("name", "")
        if name not in {"search", "images", "llbot_auth", "pmhq", "onebot"} and not name.startswith("provider:"):
            raise ValueError("凭据类型无效")
        service.config.vault.set(name, payload.get("value", ""))
        return {"saved": True}

    @api.post("/api/models/catalog")
    async def model_catalog(request: Request):
        return await service.models.catalog(await request.json())

    @api.post("/api/models/credential-status")
    async def credential_status(request: Request):
        spec = await request.json()
        return {"saved": bool(service.config.provider_key(spec)), "plaintext_returned": False}

    @api.get("/api/conversations")
    async def conversations():
        from .conversation_records import conversations as read
        return read(service.store, service.config.get("account", "id"))

    @api.get("/api/messages")
    async def messages(scope: str, before: float = 1e20, before_id: str = "", q: str = "", anchor: str = ""):
        from .conversation_records import messages as read
        return read(service.store, service.config.get("account", "id"), scope, before, before_id, q.strip()[:200], anchor)

    @api.get("/api/tasks")
    async def tasks():
        return service.store.rows("SELECT * FROM tasks ORDER BY created DESC LIMIT 300")

    @api.get("/api/schedules")
    async def schedules():
        return {"rows": service.schedules.listing(), "policy": service.config.get("reminders")}

    @api.post("/api/schedules/policy")
    async def schedule_policy(request: Request):
        changes = await request.json()
        if not isinstance(changes, dict) or set(changes)-{"catch_up_once", "catch_up_recurring"}:
            raise ValueError("补发设置无效")
        result = service.config.update({"reminders": changes})
        service.store.event("config", "管理员调整提醒补发策略", changes)
        service.schedules.wake.set()
        return result["reminders"]

    @api.get("/api/schedules/{ident}")
    async def schedule_detail(ident: str):
        row = service.schedules.get(ident)
        return {"schedule": {key: value for key, value in row.items() if key not in {"source", "peer"}},
                "runs": service.store.rows("SELECT * FROM future_runs WHERE schedule_id=? ORDER BY started DESC LIMIT 100", (ident,))}

    @api.post("/api/schedules/{ident}")
    async def change_schedule(ident: str, request: Request):
        spec = await request.json()
        result = await service.schedules.change(ident, spec.get("action"), spec.get("changes"))
        service.store.event("reminder", "管理员管理未来安排", {"id": ident, "action": spec.get("action")})
        return {"status": result["status"]}

    @api.get("/api/tasks/{ident}")
    async def task_detail(ident: str):
        row = service.tasks.get(ident)
        return scrub({"task": row, "steps": service.store.rows("SELECT * FROM task_steps WHERE task_id=? ORDER BY started", (ident,)), "collaborators": service.store.rows("SELECT * FROM collaborators WHERE task_id=?", (ident,)), "files": service.store.rows("SELECT * FROM files WHERE task_id=?", (ident,)), "recovery": service.recovery.options(ident), "requests": service.store.rows("SELECT id,status,stage,created,finished FROM requests WHERE task_id=? ORDER BY created", (ident,))})

    @api.post("/api/tasks/{ident}/recover")
    async def recover_task(ident: str, request: Request):
        body = await request.json()
        return await service.recovery.continue_task(ident, body.get("mode", "continue"), body.get("confirm_unknown") is True)

    @api.get("/api/requests/{ident}")
    async def request_detail(ident: str):
        detail = service.diagnostics.details(ident)
        if detail["request"]["bot"] != service.config.get("account", "id"):
            raise PermissionError("请求不属于当前机器人")
        for memory in detail["memories"]:
            live = service.store.one("SELECT text,updated FROM memories WHERE id=?", (memory["memory_id"],))
            memory["current"] = live
            snapshot = json.loads(memory["snapshot"])
            sources = []
            try:
                evidence = json.loads(snapshot.get("evidence") or "[]")
            except (ValueError, TypeError):
                evidence = []
            for source_id in evidence:
                source = service.store.one("SELECT id,scope,name,sender,text,created FROM messages WHERE id=? AND bot=?", (source_id, detail["request"]["bot"]))
                if source:
                    sources.append(source)
            memory["sources"] = sources
        if detail["request"].get("task_id"):
            detail["recovery"] = service.recovery.options(detail["request"]["task_id"])
            detail["files"] = service.store.rows("SELECT id,name,status,kind FROM files WHERE task_id=?", (detail["request"]["task_id"],))
        return scrub(detail)

    @api.get("/api/faults")
    async def faults(status: str = "", q: str = ""):
        clauses, args = [], []
        if status:
            if status not in {"open", "recovered", "ignored"}:
                raise ValueError("错误状态无效")
            clauses.append("status=?")
            args.append(status)
        if q:
            clauses.append("(instr(title,?)>0 OR instr(reason,?)>0)")
            args += [q[:200], q[:200]]
        return scrub({"counts": {r["status"]: r["n"] for r in service.store.rows("SELECT status,COUNT(*) AS n FROM faults GROUP BY status")}, "rows": service.store.rows("SELECT * FROM faults"+(" WHERE "+" AND ".join(clauses) if clauses else "")+" ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'recovered' THEN 1 ELSE 2 END,last_seen DESC LIMIT 300", args)})

    @api.get("/api/faults/{ident}")
    async def fault_detail(ident: str):
        row = service.store.one("SELECT * FROM faults WHERE id=?", (ident,))
        if not row:
            raise ValueError("错误记录不存在")
        return scrub({"fault": row, "occurrences": service.store.rows("SELECT o.*,r.scope FROM fault_occurrences o LEFT JOIN requests r ON r.id=o.request_id WHERE fault_id=? ORDER BY created DESC LIMIT 100", (ident,))})

    @api.post("/api/faults/{ident}")
    async def manage_fault(ident: str, request: Request):
        op = (await request.json()).get("action")
        if op not in {"ignore", "reopen"}:
            raise ValueError("可以忽略或重新关注，不能把未验证的问题直接标为已恢复")
        if not service.store.one("SELECT id FROM faults WHERE id=?", (ident,)):
            raise ValueError("错误记录不存在")
        state = "ignored" if op == "ignore" else "open"
        service.store.execute("UPDATE faults SET status=? WHERE id=?", (state, ident))
        service.store.execute("UPDATE fault_occurrences SET status=? WHERE fault_id=? AND status IN ('open','ignored')", (state, ident))
        return {"status": state}

    @api.get("/api/artifacts/{ident}")
    async def artifact_versions(ident: str):
        service.files.resolve(ident, admin=True)
        detail = service.artifacts.details(ident)
        for version in detail["versions"]:
            version["source_files"] = [r for file_id in json.loads(version["sources"]) if (r := service.store.one("SELECT id,name,kind,status FROM files WHERE id=?", (file_id,)))]
        return scrub(detail)

    @api.post("/api/tasks/{ident}/cancel")
    async def cancel_task(ident: str):
        service.tasks.cancel(ident)
        return {"cancelled": True}

    @api.get("/api/files")
    async def files():
        rows = service.store.rows("SELECT * FROM files ORDER BY created DESC LIMIT 500")
        for row in rows:
            version = service.artifacts.version(row["id"])
            row["version"] = {k: version[k] for k in ("number", "artifact_id", "parent_file")} if version else None
        return rows

    @api.get("/api/files/{ident}/download")
    async def download_file(ident: str):
        row, path = service.files.resolve(ident, admin=True)
        return FileResponse(path, filename=row["name"])

    @api.get("/api/files/{ident}/preview")
    async def preview_file(ident: str):
        row, path = service.files.resolve(ident, admin=True)
        mime = __import__("mimetypes").guess_type(row["name"])[0] or "application/octet-stream"
        if mime == "image/tiff":
            def raster_preview():
                import io
                from PIL import Image, ImageOps
                with Image.open(path) as source:
                    image = ImageOps.exif_transpose(source)
                    image.thumbnail((2048, 2048))
                    output = io.BytesIO()
                    image.convert("RGB").save(output, "PNG")
                    return output.getvalue()
            return Response(await asyncio.to_thread(raster_preview), media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "application/pdf", "video/mp4", "video/webm"}:
            return FileResponse(path, media_type="application/octet-stream", filename=row["name"])
        return FileResponse(path, media_type=mime, headers={"X-Content-Type-Options": "nosniff"})

    @api.post("/api/files/{ident}/favorite")
    async def favorite(ident: str, request: Request):
        service.files.resolve(ident, admin=True)
        value = bool((await request.json()).get("value"))
        service.store.execute("UPDATE files SET favorite=? WHERE id=?", (int(value), ident))
        return {"favorite": value}

    @api.get("/api/files/{ident}/resend-state")
    async def resend_state(ident: str):
        service.files.resolve(ident, admin=True)
        last = service.store.one("SELECT status FROM deliveries WHERE file_id=? ORDER BY created DESC,id DESC LIMIT 1", (ident,))
        return {"unknown": bool(last and last["status"] == "unknown")}

    @api.post("/api/files/{ident}/resend")
    async def resend(ident: str, request: Request):
        row, _ = service.files.resolve(ident, admin=True)
        from .domain import Message
        from .diagnostics import REQUEST, TASK
        body = await request.json()
        last = service.store.one("SELECT * FROM deliveries WHERE file_id=? ORDER BY created DESC,id DESC LIMIT 1", (ident,))
        if last and last["status"] in {"queued", "sending"}:
            raise ValueError("此文件已经在发送，请等待结果")
        if last and last["status"] == "unknown" and not body.get("confirm_unknown"):
            raise ValueError("上次送达未确认，请先核对QQ后确认重发")
        if not service.online or service.stopping:
            raise ValueError("QQ尚未连接，请先恢复连接")
        message = Message(uid("resend_"), row["bot"], row["scope"], row["owner"], "用户", "重新发送文件", row["scope"].startswith("private:"))
        service.diagnostics.begin(message, parent=(last or {}).get("request_id"))
        token = REQUEST.set(message.request_id)
        task_token = TASK.set(row.get("task_id"))
        try:
            if row.get("task_id"):
                service.diagnostics.attach_task(row["task_id"])
            await service.delivery.enqueue(message, file=ident)
            service.store.execute("DELETE FROM file_refs WHERE file_id=? AND target_kind='delivery_failed'", (ident,))
            service.diagnostics.finish(message.request_id)
        except Exception as exc:
            service.diagnostics.finish(message.request_id, "failed", str(exc))
            raise
        finally:
            TASK.reset(task_token)
            REQUEST.reset(token)
        return {"sent": True}

    @api.get("/api/memories")
    async def memories():
        return service.store.rows("SELECT * FROM memories ORDER BY updated DESC LIMIT 1000")

    @api.get("/api/memories/{ident}")
    async def memory_detail(ident: str):
        row = service.store.one("SELECT * FROM memories WHERE id=? AND bot=?", (ident, service.config.get("account", "id")))
        if not row:
            raise ValueError("原记忆已删除")
        return scrub(row)

    @api.post("/api/memories/{ident}")
    async def edit_memory(ident: str, request: Request):
        body = await request.json()
        service.memory.edit(ident, "@admin", body.get("text"), admin=True)
        return {"saved": True}

    @api.get("/api/persona")
    async def persona():
        return service.store.rows("SELECT * FROM persona ORDER BY created")

    @api.post("/api/persona")
    async def edit_persona(request: Request):
        body = await request.json()
        if "restore" in body:
            row = service.store.one("SELECT * FROM persona WHERE id=?", (body["restore"],))
            if not row:
                raise ValueError("人格版本不存在")
            return service.personality.save(row["text"], json.loads(row["dimensions"]), "管理员恢复版本 "+row["id"], json.loads(row["evidence"]))
        old = service.personality.current()
        return service.personality.save(body["text"], body.get("dimensions", json.loads(old["dimensions"])), body.get("reason", "管理员编辑"), [])

    @api.get("/api/usage")
    async def usage():
        return {"calls": service.store.rows("SELECT * FROM usage ORDER BY created DESC LIMIT 300"), "users": service.store.rows("SELECT owner,COUNT(*) AS calls,SUM(input+output) AS tokens,SUM(CASE WHEN source!='image' AND (input IS NULL OR output IS NULL) THEN 1 ELSE 0 END) AS unknown,SUM(cost) AS cost FROM usage WHERE created>? GROUP BY owner", (time.time()-86400,)), "policy": service.config.get("usage")}

    @api.post("/api/usage/reset")
    async def reset_usage(request: Request):
        body = await request.json()
        owners = [body["owner"]] if body.get("owner") else [r["owner"] for r in service.store.rows("SELECT DISTINCT owner FROM usage")]
        for owner in owners:
            service.store.checkpoint("quota_reset:"+service.config.get("account", "id")+":"+owner, time.time())
        service.store.event("usage", "本地用户限额已重置", {"owners": owners, "provider_allowance_unchanged": True})
        return {"reset": len(owners)}

    @api.get("/api/extensions")
    async def extensions():
        return service.store.rows("SELECT * FROM extensions ORDER BY created DESC")

    @api.post("/api/extensions/install")
    async def install(request: Request):
        body = await request.json()
        return await service.extensions.install(body["source"], "@admin", subpath=body.get("subpath", ""))

    @api.post("/api/extensions/{ident}/toggle")
    async def toggle_extension(ident: str, request: Request):
        await service.extensions.toggle(ident, bool((await request.json())["enabled"]))
        return {"saved": True}

    @api.post("/api/extensions/{ident}/dependencies")
    async def dependencies(ident: str, request: Request):
        return await service.extensions.dependencies(ident, bool((await request.json()).get("allow_updates")))

    @api.get("/api/extensions/{ident}/config")
    async def extension_config(ident: str):
        return service.extensions.configuration(ident)

    @api.post("/api/extensions/{ident}/config")
    async def save_extension_config(ident: str, request: Request):
        await service.backups.create("before-plugin-config", False)
        result = service.extensions.configuration(ident, await request.json())
        row, _ = service.extensions._row(ident)
        if row["enabled"]:
            await service.astrbot.reload_plugin(ident)
        return result

    @api.post("/api/extensions/{ident}/update")
    async def update_extension(ident: str):
        await service.backups.create("before-plugin-update")
        return await service.extensions.update(ident)

    @api.post("/api/extensions/{ident}/rollback")
    async def rollback_extension(ident: str):
        return await service.extensions.rollback(ident)

    @api.post("/api/extensions/{ident}/uninstall")
    async def uninstall_extension(ident: str):
        return await service.extensions.uninstall(ident)

    @api.get("/api/backups")
    async def backups():
        return [{"name": p.name, "size": p.stat().st_size, "created": p.stat().st_mtime} for p in sorted(service.backups.root.glob("*.zip"), reverse=True)]

    @api.post("/api/backups")
    async def backup():
        return await service.backups.create()

    @api.post("/api/backups/restore")
    async def restore(request: Request):
        return await service.backups.prepare_restore((await request.json())["name"])

    @api.get("/api/events")
    async def events():
        return service.store.rows("SELECT * FROM events ORDER BY created DESC LIMIT 500")

    @api.get("/api/search")
    async def search(q: str):
        if not q.strip():
            return []
        needle = "%"+q.replace("%", "\\%").replace("_", "\\_")+"%"
        result = []
        for table, column, route, kind in (("messages", "text", "conversations", "消息"), ("memories", "text", "memory", "记忆"), ("tasks", "title", "tasks", "任务"), ("files", "name", "files", "文件")):
            rows = service.store.rows(f"SELECT * FROM {table} WHERE {column} LIKE ? ESCAPE '\\' LIMIT 20", (needle,))
            for row in rows:
                result.append({"id": row["id"], "route": route, "kind": kind, "title": row[column][:150], "detail": row.get("scope", "")+" · "+str(row.get("owner", row.get("sender", "")))})
        return result

    @api.post("/api/plan/login")
    async def plan_login():
        await service.services.plan_credentials()
        return {"url": "http://127.0.0.1:24762/companion/start"}

    @api.get("/plan/open")
    async def open_plan(request: Request):
        # A real top-level browser navigation keeps OAuth cookies in the callback browser.
        if not secrets.compare_digest(request.cookies.get("companion_session", ""), session):
            return RedirectResponse("/#models", 303)
        try:
            data = await plan_login()
            return RedirectResponse(data["url"], 303)
        except Exception as exc:
            service.store.event("service", "GPT登录入口启动失败", {"error": str(exc)}, "error")
            reason = html.escape(str(exc)[:600])
            return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Sol · GPT登录</title>'
                                '<style>body{background:#faf8f3;color:#302b25;font:16px/1.8 system-ui;margin:60px auto;max-width:680px;padding:24px}'
                                'a{color:#97472d}p{white-space:pre-wrap}</style><h1>GPT登录未能打开</h1><p>'+reason+
                                '</p><p>可在Sol活动记录和引擎日志中查看本次失败。</p><a href="/plan/open">重新打开登录</a> · '
                                '<a href="/#models">返回模型设置</a></html>', status_code=503)

    @api.post("/api/plan/resume")
    async def plan_resume():
        await service.services.plan_credentials()
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            await client.get("http://127.0.0.1:24762/")
            response = await client.post("http://127.0.0.1:24762/admin/resume")
            response.raise_for_status()
        return {"resumed": True, "provider_allowance_unchanged": True}

    @api.get("/api/plan/status")
    async def plan_status():
        from .config import Vault
        folder = service.store.root / "plan"
        settings_file = folder / "settings.bin"
        credentials_file = folder / "credentials.bin"
        if not settings_file.is_file():
            return {"started": False, "signed_in": False}
        settings = json.loads(Vault._protect(settings_file.read_bytes(), True))
        credentials = json.loads(Vault._protect(credentials_file.read_bytes(), True)) if credentials_file.is_file() else {}
        active = credentials.get("accounts", {}).get(credentials.get("active"), {})
        for provider in service.config.get("models", "providers"):
            if provider.get("kind") == "plan":
                service.config.vault.set("provider:"+provider["id"], settings["bridge_key"])
        return {"started": True, "signed_in": bool(active), "email": active.get("email"), "paused": settings.get("paused", False)}

    @api.post("/api/services/{name}/{action}")
    async def service_action(name: str, action: str):
        if name == "qq" and action in {"show-window", "hide-window"}:
            if service.config.get("account", "driver") != "native":
                raise ValueError("此入口仅管理 Sol 独立 QQ 窗口")
            return await service.services.native_qq.rpc("window", {"visible": action == "show-window"})
        if name == "qq" and action in {"start", "stop"}:
            if action == "start":
                if not service.config.get("account", "enabled"):
                    service.config.update({"account": {"enabled": True}})
                if service.astrbot.applied_account != service.config.get("account") or service.astrbot.context is None:
                    await service.disconnected("管理员应用QQ接入配置")
                    if service.astrbot.context is not None:
                        await service.astrbot.stop()
                    await service.astrbot.start()
                    if service.astrbot.context is None:
                        raise RuntimeError("内部核心启动失败："+service.astrbot.error)
            return await service.services.qq(action)
        if name == "astrbot" and action == "restart":
            await service.disconnected("管理员重启核心")
            await service.astrbot.stop()
            await service.astrbot.start()
            if service.astrbot.context is not None:
                service.background_task(service.personality.reflect_on_start(service.local), "personality-restart")
            return {"started": service.astrbot.context is not None}
        if name == "plan" and action == "start":
            await service.services.start_bridge()
            return {"url": "http://127.0.0.1:24762"}
        raise ValueError("服务操作不存在")

    @api.get("/api/qq/status")
    async def qq_status():
        return await service.services.qq_status()

    @api.post("/api/qq/qrcode")
    async def qq_qrcode():
        return await service.services.qq_qrcode()

    @api.get("/api/gpu")
    async def gpu():
        command = shutil.which("nvidia-smi")
        if not command:
            return {"available": False}
        process = await asyncio.create_subprocess_exec(command, "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 3)
            if process.returncode:
                return {"available": False}
            fields = out.decode().strip().split(", ")
            return {"available": True, "name": fields[0], "used_mb": float(fields[1]), "total_mb": float(fields[2]), "utilization": float(fields[3]), "temperature": float(fields[4])}
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    @api.websocket("/onebot")
    async def onebot(downstream: WebSocket):
        import aiohttp
        if downstream.headers.get("authorization") != "Bearer "+service.config.vault.ensure("onebot") or downstream.headers.get("x-self-id") != service.config.get("account", "qq"):
            await downstream.close(code=1008)
            return
        if service.connection_token:
            await downstream.close(code=1013, reason="已有指定机器人接入连接")
            return
        connection_token = secrets.token_urlsafe(16)
        service.connection_token = connection_token
        try:
            await downstream.accept()
        except BaseException:
            service.connection_token = None
            raise
        send_lock = asyncio.Lock()
        account_verified = False
        last_health = time.monotonic()
        host_data = service.services.qq_root
        def translate(value, outgoing=False):
            if isinstance(value, list):
                return [translate(v, outgoing) for v in value]
            if not isinstance(value, dict):
                return value
            result = {}
            for key, item in value.items():
                if key in {"file", "path"} and isinstance(item, str):
                    if not outgoing and item.startswith("/exchange/"):
                        path = (host_data / "exchange" / item.removeprefix("/exchange/")).resolve()
                        item = str(path) if path.is_relative_to((host_data / "exchange").resolve()) else ""
                    elif outgoing and account.get("driver") == "legacy_llbot":
                        raw = unquote(item.removeprefix("file:///"))
                        path = Path(raw)
                        if path.is_absolute() and path.is_file():
                            path = path.resolve()
                            if not path.is_relative_to(service.store.root):
                                raise PermissionError("不能发送软件数据目录之外的文件")
                            target = host_data / "exchange" / (hashlib.sha256(path.read_bytes()).hexdigest()+path.suffix)
                            if not target.exists():
                                shutil.copy2(path, target)
                            item = "/exchange/"+target.name
                result[key] = translate(item, outgoing)
            return result
        try:
            async with aiohttp.ClientSession() as session_http:
                port = service.config.get("account", "onebot_port")
                async with session_http.ws_connect(f"http://127.0.0.1:{port}/ws", headers={"Authorization": "Bearer "+service.config.vault.ensure("onebot"), "X-Self-ID": service.config.get("account", "qq"), "X-Client-Role": "Universal", "X-OneBot-Version": "11"}, max_msg_size=64*1024*1024) as upstream:
                    async def to_core():
                        nonlocal account_verified, last_health
                        while True:
                            data = await downstream.receive_json()
                            echo = str(data.get("echo", ""))
                            if echo.startswith("companion:login"):
                                info = data.get("data") or {}
                                if str(info.get("user_id")) != service.config.get("account", "qq"):
                                    raise PermissionError("接入账号与指定机器人不一致，已拒绝")
                                account_verified = True
                            elif echo.startswith("companion:status"):
                                last_health = time.monotonic()
                                status = data.get("data") or {}
                                if account_verified and status.get("online"):
                                    await service.connected()
                                elif service.online:
                                    await service.disconnected("QQ 登录失效或离线")
                            else:
                                if data.get("post_type") and str(data.get("self_id")) != service.config.get("account", "qq"):
                                    raise PermissionError("拒绝非指定账号的消息")
                                if account_verified:
                                    await upstream.send_json(translate(data))
                    async def to_qq():
                        async for packet in upstream:
                            if packet.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(packet.data)
                                if account.get("driver") != "native" and data.get("action", "").startswith(("send_", "upload_group_file", "upload_private_file")):
                                    await service.services.pace_send()
                                async with send_lock:
                                    await downstream.send_json(translate(data, True))
                    async def monitor():
                        while True:
                            if time.monotonic()-last_health > 40:
                                raise ConnectionError("QQ接入未响应状态查询，连接已失去响应")
                            async with send_lock:
                                await downstream.send_json({"action": "get_login_info", "params": {}, "echo": "companion:login"})
                                await downstream.send_json({"action": "get_status", "params": {}, "echo": "companion:status"})
                            await asyncio.sleep(10)
                    handles = [asyncio.create_task(f()) for f in (to_core, to_qq, monitor)]
                    done, pending = await asyncio.wait(handles, return_when=asyncio.FIRST_COMPLETED)
                    for handle in pending:
                        handle.cancel()
                    await asyncio.gather(*handles, return_exceptions=True)
        except Exception as exc:
            service.store.event("connection", "QQ 接入连接结束", {"error": str(exc)}, "warning")
        finally:
            if service.connection_token == connection_token:
                service.connection_token = None
                await service.disconnected("QQ 连接断开")
    return api
