"""Single authoritative configuration and encrypted credentials."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import secrets
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("COMPANION_DATA", str(ROOT / "state"))).resolve()
RESOURCES = Path(os.environ.get("SOL_RESOURCE_ROOT", str(DATA / "resources"))).resolve()
DEFAULTS = {
    "app": {"name": "Sol", "port": 24760, "autostart": True, "backup_hours": 24, "backup_keep": 14, "theme": "light"},
    "account": {"id": "main", "qq": "", "admins": [], "enabled": False, "auto_login": True, "driver": "native", "onebot_port": 24761, "webui_port": 3085, "pmhq_image": "linyuchen/pmhq:9.0.6", "llbot_image": "linyuchen/llbot:8.3.2"},
    "access": {"mode": "all", "users": [], "groups": [], "group_overrides": {}},
    "chat": {"batch": True, "wait_seconds": 1.8, "max_wait_seconds": 6, "interrupt": True, "max_bubbles": 10, "send_interval": [2, 3], "natural": True, "waiting_face": True},
    "social": {"enabled": True, "idle_topics": False, "cooldown": 120, "hourly_limit": 5, "quiet_hours": [0, 8]},
    "models": {"selected": "", "providers": [], "local_url": "http://127.0.0.1:24763", "local_model": "gemma4:e2b", "local_enabled": True, "local_timeout": 12},
    "tasks": {"parallel": 4, "per_user": 2, "attempts": 3, "timeout": 1800, "max_steps": 16, "autonomous_steps": 8, "max_pending": 100, "token_limit": 300000, "autonomous_token_limit": 80000, "type_limits": {}},
    "reminders": {"catch_up_once": True, "catch_up_recurring": False},
    "usage": {"user_daily": 200000, "public_daily": 400000, "overrides": {}, "unknown_blocks": True},
    "files": {"normal_days": 30, "temporary_days": 2, "raw_days": 30, "max_mb": 100, "python_idle_seconds": 300},
    "images": {"order": ["local"], "fallback": True, "local_url": "http://127.0.0.1:24663", "remote_url": "", "remote_model": "", "local_root": str(ROOT / "runtime/images"), "comfy_root": str(ROOT / "runtime/images/ComfyUI")},
    "video": {"max_minutes": 120, "max_mb": 200, "whisper_model": "", "ffmpeg": "ffmpeg", "node": "node"},
    "search": {"kind": "tavily", "endpoint": "https://api.tavily.com/search", "cache_seconds": 300},
    "native": {"enabled": True, "search_mode": "auto", "features": {}, "backends": {}, "mcp_servers": [], "skill_ids": [], "browser_domains": []},
    "permissions": {"install_extensions": True, "tools": True, "tune_resources": True, "tune_chat": True, "change_model": False, "change_limits": False, "change_permissions": False},
    "features": {name: True for name in ("chat", "groups", "memory", "models", "reuse", "images", "documents", "python", "video", "tasks", "usage", "plugins")},
    "layout": {"widgets": []},
}


class Config:
    def __init__(self, data: Path = DATA):
        self.data = data
        self.data.mkdir(parents=True, exist_ok=True)
        self.path = data / "settings.json"
        self.lock = threading.RLock()
        self.value = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            self._merge(self.value, json.loads(self.path.read_text("utf-8")))
        else:
            self.save()
        # A packaged install has prepared resources outside mutable user records.
        speech = RESOURCES / "whisper-small"
        if not self.value["video"]["whisper_model"] and (speech / "model.bin").is_file():
            self.value["video"]["whisper_model"] = str(speech)
            self.save()
        self.vault = Vault(data)

    @staticmethod
    def _merge(target, incoming):
        for key, value in incoming.items():
            if key != "overrides" and isinstance(value, dict) and isinstance(target.get(key), dict):
                Config._merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.value)

    def get(self, section, key=None):
        with self.lock:
            return copy.deepcopy(self.value[section] if key is None else self.value[section].get(key))

    @staticmethod
    def credential_name(kind, base_url):
        from urllib.parse import urlsplit
        parts = urlsplit(base_url.strip().rstrip("/"))
        identity = kind+":"+parts.scheme.lower()+"://"+parts.netloc.lower()+parts.path
        return "model-service:"+hashlib.sha256(identity.encode()).hexdigest()

    def provider_key(self, provider):
        kind, base = provider.get("kind", "openai"), provider.get("base_url", "").strip().rstrip("/")
        rows = self.get("models", "providers")
        matches = [p for p in rows if p.get("kind", "openai") == kind and p.get("base_url", "").strip().rstrip("/") == base]
        own = next((p for p in matches if p["id"] == provider.get("id")), None)
        if own and self.vault.get("provider:"+own["id"]):
            return self.vault.get("provider:"+own["id"])
        saved = self.vault.get(self.credential_name(kind, base))
        if saved:
            return saved
        return next((self.vault.get("provider:"+p["id"]) for p in matches if p.get("remember_key", True) and self.vault.get("provider:"+p["id"])), "")

    def save(self):
        with self.lock:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.value, ensure_ascii=False, indent=2), "utf-8")
            temp.replace(self.path)

    def update(self, changes):
        if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
            raise ValueError("配置字段无效")
        candidate = self.snapshot()
        self._merge(candidate, changes)
        if any(type(candidate["reminders"][key]) is not bool for key in ("catch_up_once", "catch_up_recurring")):
            raise ValueError("提醒补发开关必须为布尔值")
        from .native_capabilities import validate_config
        validate_config(candidate["native"])
        native_secrets = {}
        if "native" in changes:
            old_servers = {s["id"]: s for s in self.get("native", "mcp_servers")}
            for server in candidate["native"]["mcp_servers"]:
                token = server.pop("token", None)
                if token is not None:
                    native_secrets["native-mcp:"+server["id"]] = token
                elif old_servers.get(server["id"], {}).get("url") != server["url"]:
                    native_secrets["native-mcp:"+server["id"]] = ""
        if any(candidate["features"].get(name) is False for name in ("models", "tasks", "usage")):
            raise ValueError("模型网关、任务调度和用量记账是基础服务，不能单独关闭；可关闭对应工具权限")
        if candidate["access"]["mode"] not in {"all", "selected"}:
            raise ValueError("使用范围无效")
        for section, keys in {"tasks": ["parallel", "per_user", "attempts", "timeout", "max_steps"], "chat": ["wait_seconds", "max_wait_seconds"]}.items():
            for key in keys:
                if not isinstance(candidate[section][key], (int, float)) or candidate[section][key] <= 0:
                    raise ValueError(f"{section}.{key} 必须为正数")
        if not 1024 <= int(candidate["app"]["port"]) <= 65535:
            raise ValueError("端口无效")
        if candidate["app"]["theme"] not in ("light", "dark"):
            raise ValueError("外观只能选择浅色或暗色")
        if not isinstance(candidate["images"]["order"], list) or not candidate["images"]["order"] or any(v not in {"local", "remote", "native"} for v in candidate["images"]["order"]):
            raise ValueError("图片后端顺序只支持local、remote、native")
        if len(candidate["account"]["admins"]) > 50:
            raise ValueError("管理员数量超出范围")
        if not isinstance(candidate["account"]["auto_login"], bool):
            raise ValueError("QQ自动登录开关必须为布尔值")
        from urllib.parse import urlparse
        if urlparse(candidate["models"]["local_url"]).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("本地辅助模型地址必须为本机回环地址")
        for section, keys in {"usage": ["user_daily", "public_daily"], "tasks": ["token_limit", "autonomous_token_limit"], "files": ["normal_days", "temporary_days", "raw_days", "max_mb", "python_idle_seconds"]}.items():
            for key in keys:
                if not isinstance(candidate[section][key], (int, float)) or candidate[section][key] < 0:
                    raise ValueError(f"{section}.{key} 必须为非负数")
        for value in candidate["usage"]["overrides"].values():
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError("个人限额必须是非负数")
        bubbles = candidate["chat"]["max_bubbles"]
        if type(bubbles) is not int or bubbles < 0:
            raise ValueError("气泡上限必须为非负整数，0表示不限制数量")
        intervals = candidate["chat"]["send_interval"]
        if (not isinstance(intervals, list) or len(intervals) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in intervals)
                or not 0 <= intervals[0] <= intervals[1] <= 60):
            raise ValueError("发送间隔需要为0～60秒，最短间隔不能大于最长间隔")
        if any(not isinstance(h, (int, float)) or not 0 <= h <= 24 for h in candidate["social"]["quiet_hours"]) or len(candidate["social"]["quiet_hours"]) != 2:
            raise ValueError("免打扰时段无效")
        ids = [p.get("id") for p in candidate["models"]["providers"]]
        if len(ids) != len(set(ids)):
            raise ValueError("模型配置 ID 不能重复")
        for provider in candidate["models"]["providers"]:
            provider.pop("native_capabilities", None)
            provider.pop("effective_capabilities", None)
            if not provider.get("id") or not provider.get("model"):
                raise ValueError("模型配置缺少 ID 或型号")
            if urlparse(provider.get("base_url", "")).scheme not in {"http", "https"}:
                raise ValueError("模型接口地址无效")
            address = urlparse(provider["base_url"])
            if not address.hostname or address.username or address.password or address.query or address.fragment:
                raise ValueError("API地址不能包含账号、查询参数或密钥，请将密钥填入API Key输入框")
            if "providers" in changes.get("models", {}):
                if provider.get("kind") == "gemini":
                    provider["model"] = provider["model"].removeprefix("models/")
                from .reasoning import validate
                provider["effort"] = validate(provider, provider.get("effort", "default"))
                supplied = provider.pop("key", "")
                secret = supplied.strip() if supplied else self.provider_key(provider)
                if provider.get("kind") != "plan":
                    self.vault.set("provider:"+provider["id"], secret)
                    if secret and provider.get("remember_key", True):
                        self.vault.set(self.credential_name(provider.get("kind", "openai"), provider["base_url"]), secret)
        with self.lock:
            for key, secret in native_secrets.items():
                self.vault.set(key, secret)
            self.value = candidate
            self.save()
        return self.snapshot()


class Vault:
    def __init__(self, root):
        from cryptography.fernet import Fernet
        key_path = root / "vault.key"
        if not key_path.exists():
            key = Fernet.generate_key()
            key_path.write_bytes(self._protect(key))
        self.cipher = Fernet(self._protect(key_path.read_bytes(), decrypt=True))
        self.path = root / "secrets.bin"
        self.values = json.loads(self.cipher.decrypt(self.path.read_bytes())) if self.path.exists() else {}
        self.lock = threading.RLock()

    @staticmethod
    def _protect(value, decrypt=False):
        if os.name != "nt":
            return value
        import ctypes
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
        buf = ctypes.create_string_buffer(value)
        src = Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        dst = Blob()
        api = ctypes.windll.crypt32
        ok = api.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)) if decrypt else api.CryptProtectData(ctypes.byref(src), "Companion Studio", None, None, None, 1, ctypes.byref(dst))
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(dst.data, dst.length)
        finally:
            ctypes.windll.kernel32.LocalFree(dst.data)

    def get(self, name, default=""):
        with self.lock:
            return self.values.get(name, default)

    def set(self, name, value):
        with self.lock:
            self.values[name] = value
            temp = self.path.with_suffix(".tmp")
            temp.write_bytes(self.cipher.encrypt(json.dumps(self.values).encode()))
            temp.replace(self.path)

    def ensure(self, name):
        value = self.get(name)
        if not value:
            value = secrets.token_urlsafe(32)
            self.set(name, value)
        return value
