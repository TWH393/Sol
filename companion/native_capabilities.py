"""Declared provider contracts. This describes integration, not a live access test."""
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

LABELS = {"search": "联网搜索", "fetch": "网页读取", "vision": "识图", "file_input": "云端文件输入",
          "code_interpreter": "云端代码解释器", "image_generation": "原生生图与编辑", "file_search": "云端文件检索",
          "shell": "云端Shell", "skills": "云端Skills", "mcp": "远程MCP", "computer": "独立浏览器操作",
          "tool_search": "按需加载工具", "programmatic": "合并独立查询与读取", "image_reuse": "图片上传复用"}

LOCAL_TOOLS = {"run_python": "code_interpreter", "create_document": "code_interpreter", "translate_pdf": "code_interpreter",
    "run_shell": "shell", "install_packages": "shell", "read_document": "file_input", "render_document_pages": "file_input", "ocr_document": "file_input",
    "search_documents": "file_search", "read_skill": "skills", "run_skill": "skills", "install_extension": "skills",
    "list_mcp_tools": "mcp", "connect_mcp": "mcp", "call_mcp": "mcp", "browse_public": "computer"}


def local_available(config, capability):
    if capability in {"code_interpreter", "shell"}:
        return bool(config.get("features", {}).get("python", True) and shutil.which("docker"))
    return capability in {"file_input", "file_search", "skills", "mcp", "computer", "tool_search", "programmatic"}


def backend_choice(config, provider, capability):
    native = config["native"]
    mode = native.get("backends", {}).get(capability, "auto")
    if mode in {"local", "native"}:
        return mode
    if local_available(config, capability):
        return "local"
    if capabilities(provider, native)[capability]["enabled"]:
        return "native"
    return "local" if capability in {"code_interpreter", "shell", "file_input", "file_search", "skills", "mcp", "computer", "tool_search", "programmatic"} else "native"


def effective_capabilities(app, provider):
    """Cheap declared readiness only; never launch tools, browsers or inference to fill a page."""
    cfg = app.config.snapshot()
    native = capabilities(provider, cfg["native"])
    labels = {**LABELS, "file_input": "文件输入与读取", "code_interpreter": "代码解释器", "file_search": "文件正文检索", "image_generation": "生图与编辑", "shell": "Shell", "skills": "Skills"}
    descriptions = {"file_input": "本地读取PDF/Office和文本，扫描页按需Docker OCR", "code_interpreter": "任务专属Docker，保留变量和中间文件；首次准备常用包镜像", "file_search": "本地正文索引，返回文件、页码、段落或工作表", "shell": "与Python共用任务Docker，可下载和安装依赖", "skills": "自主查找、安装和读取技能；脚本在任务Docker执行", "mcp": "Sol通用MCP客户端；没有服务时可自主寻找并连接公开服务", "computer": "独立匿名浏览器，只处理公开页面；需要登录时说明并寻找替代来源", "tool_search": "闲聊携带短能力目录，任务按需加载说明", "programmatic": "独立只读查询并行，同任务结果复用；写操作按顺序", "image_reuse": "识图笔记本地复用；兼容服务支持上传ID复用，仍可能收图片输入费用"}
    result = {}
    for key, label in labels.items():
        mode = cfg["native"].get("backends", {}).get(key, "auto")
        backend = backend_choice(cfg, provider, key)
        available = local_available(cfg, key) if backend == "local" else native[key]["enabled"]
        reason = descriptions.get(key, native[key]["reason"]) if backend == "local" else native[key]["reason"]
        execution = "本地 Docker" if key in {"code_interpreter", "shell"} and backend == "local" else "Sol 本地工具" if backend == "local" else "服务商原生"
        if key == "image_generation":
            order = cfg["images"]["order"]
            backend = order[0]
            execution = {"local": "本地 ComfyUI", "remote": "远程图片接口", "native": "服务商原生"}[backend]
            available = (Path(cfg["images"]["comfy_root"], "main.py").is_file() if backend == "local" else bool(cfg["images"]["remote_url"] and cfg["images"]["remote_model"]) if backend == "remote" else native[key]["enabled"])
            reason = "沿用图片服务顺序；提示词由当前模型在规划中优化，长图排版用Docker"
        if key == "image_reuse":
            available, backend, execution = True, "local", "本地笔记 / 兼容服务上传复用"
            reason = descriptions[key]
        if key in {"file_input", "file_search", "skills"}:
            feature = "plugins" if key == "skills" else "documents"
            available = available and cfg["features"].get(feature, True)
        if key in {"code_interpreter", "shell"}:
            available = available and cfg["features"].get("python", True)
        if key == "image_generation":
            available = available and cfg["features"].get("images", True)
        if key == "computer":
            from .config import RESOURCES
            import os
            browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(RESOURCES / "browsers")))
            if backend == "local" and not browsers.exists() and not (Path.home() / "AppData/Local/ms-playwright").exists():
                available = False
                reason = "需要准备Sol配套的Playwright Chromium浏览器"
        enabled = cfg["native"]["features"].get(key, True)
        result[key] = {"label": label, "mode": mode, "backend": backend, "execution": execution,
            "status": "已关闭" if not enabled else "可用" if available else "需要配置",
            "reason": reason if available else ("需要安装并启动Docker Desktop" if key in {"code_interpreter", "shell"} and backend == "local" else reason),
            "supported": available, "enabled": enabled and available, "verified": False,
            "native_supported": native[key]["supported"], "selectable": key in {"file_input", "code_interpreter", "file_search", "shell", "skills", "mcp", "computer"}}
    return result


def family(provider):
    host = (urlsplit(provider.get("base_url", "")).hostname or "").lower()
    if provider.get("kind") == "plan":
        return "plan"
    if host == "api.deepseek.com":
        return "deepseek"
    if host == "api.openai.com" and provider.get("kind") in {"openai", "responses"}:
        return "openai"
    return "other"


def capabilities(provider, config):
    service = family(provider)
    result = {}
    for key, label in LABELS.items():
        supported = service == "openai" or service == "plan" and key in {"search", "fetch", "vision"}
        reason = "按官方接口接入；模型与账号权限须实际验收"
        if service == "deepseek":
            supported = key in {"search", "fetch"} or key in {"vision", "image_reuse"} and provider.get("model", "").startswith("deepseek-flash")
            reason = "复用DeepSeek密钥调用独立Search/Fetch" if key in {"search", "fetch"} else "Flash图片输入与Files API" if supported else "当前DeepSeek接口未提供此内建能力；可使用Sol本地工具"
        elif service == "plan" and not supported:
            reason = "当前GPT套餐桥未开放此工具；需要配置官方API模型，不能按套餐支持处理"
        elif service == "other":
            reason = "此接口未声明对应原生工具契约；已有Sol工具不受影响"
        if service == "openai" and key == "tool_search":
            version = re.match(r"gpt-(\d+)(?:\.(\d+))?", provider.get("model", ""))
            if not version or (int(version[1]), int(version[2] or 0)) < (5, 4):
                supported, reason = False, "原生Tool Search需要GPT-5.4或更新的支持型号"
        enabled = bool(config.get("enabled", True) and config.get("features", {}).get(key, True))
        result[key] = {"label": label, "supported": supported, "enabled": supported and enabled,
                       "reason": reason if enabled else "已在原生能力设置中关闭", "verified": False}
    return result


def validate_config(value):
    if not isinstance(value.get("enabled"), bool):
        raise ValueError("原生能力开关必须为布尔值")
    if value.get("search_mode") not in {"auto", "native", "tavily"}:
        raise ValueError("搜索模式只能为auto、native或tavily")
    if set(value.get("features", {})) - set(LABELS) or any(type(v) is not bool for v in value.get("features", {}).values()):
        raise ValueError("原生能力开关字段无效")
    if not isinstance(value.get("backends", {}), dict) or set(value.get("backends", {}))-set(LABELS) or any(v not in {"auto", "local", "native"} for v in value.get("backends", {}).values()):
        raise ValueError("执行方式只能选择auto、local或native")
    for field in ("browser_domains", "skill_ids"):
        if not isinstance(value.get(field), list) or any(not isinstance(v, str) or not v.strip() for v in value[field]):
            raise ValueError(field+"必须为非空文本的列表")
    for domain in value["browser_domains"]:
        if any(c in domain for c in "/:@?# ") or domain in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("浏览器域名只填公开主机名，不填网址或端口")
    servers = value.get("mcp_servers")
    if not isinstance(servers, list):
        raise ValueError("MCP服务必须为列表")
    ids = set()
    for server in servers:
        if not isinstance(server, dict):
            raise ValueError("每个MCP服务需为一个配置对象")
        label = server.get("id", "")
        url = urlsplit(server.get("url", ""))
        if not label or not label.replace("_", "").replace("-", "").isalnum() or label in ids:
            raise ValueError("MCP标识需唯一且只包含字母数字、下划线、横线")
        ids.add(label)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.port not in {None, 443} or url.fragment or url.query:
            raise ValueError("MCP必须使用无账号信息的公开HTTPS地址")
        if server.get("audience", "admins") not in {"admins", "all"}:
            raise ValueError("MCP使用范围只能是admins或all")
        if not isinstance(server.get("allowed_tools"), list) or not server["allowed_tools"] or any(not isinstance(t, str) or not t for t in server["allowed_tools"]):
            raise ValueError("MCP服务需明确填写已授权工具名单")
        if server.get("token") is not None and not isinstance(server["token"], str):
            raise ValueError("MCP授权凭据需为文本")
