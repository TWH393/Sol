"""List provider models on explicit user request; never run inference or a test prompt."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx


async def fetch_catalog(kind: str, base_url: str, key: str) -> dict:
    base = base_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("请填写不带密钥、查询参数或片段的API地址")
    gemini = kind == "gemini"
    endpoint = (base if base.endswith("/v1beta") else base+"/v1beta")+"/models" if gemini else base+"/models"
    headers = {"x-goog-api-key": key} if gemini else {"Authorization": "Bearer "+key}
    found = {}
    page_token = None
    warning = ""
    try:
        async with asyncio.timeout(50), httpx.AsyncClient(timeout=20, trust_env=kind != "plan", follow_redirects=False) as client:
            for _ in range(5):
                params = {"pageSize": 1000} if gemini else {}
                if page_token:
                    params["pageToken"] = page_token
                response = await client.get(endpoint, headers=headers, params=params)
                if response.status_code in {401, 403}:
                    raise ValueError("请先登录GPT套餐账号，再获取模型列表" if kind == "plan" else "模型列表鉴权失败，请核对API Key及账号权限")
                if response.status_code == 404:
                    raise ValueError("此API地址没有模型列表接口，请核对地址，或手动填写型号")
                if response.status_code == 429:
                    raise ValueError("服务商暂时限制了模型列表请求，请稍后再试")
                if not response.is_success:
                    raise ValueError(f"获取模型列表失败（HTTP {response.status_code}），没有发起模型推理")
                data = response.json()
                rows = data.get("models" if gemini else "data") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise ValueError("服务返回的模型列表格式无法识别，可手动填写型号")
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if gemini and "generateContent" not in row.get("supportedGenerationMethods", []):
                        continue
                    ident = row.get("name" if gemini else "id", "")
                    if not isinstance(ident, str) or not ident or len(ident) > 256:
                        continue
                    if gemini:
                        ident = ident.removeprefix("models/")
                    capability = row.get("effort") or {}
                    levels = capability.get("supported_levels", []) if isinstance(capability, dict) else []
                    levels = [s for s in levels if isinstance(s, str) and 0 < len(s) < 32]
                    label = row.get("displayName") or row.get("name") or ident
                    from .reasoning import capability
                    declared = {"model": ident.lower(), "kind": "levels", "values": levels, "source": "catalog"} if levels else {}
                    spec = capability({"kind": kind, "model": ident, "reasoning": declared})
                    found[ident] = {"id": ident, "name": str(label), "efforts": spec.get("values", []), "reasoning": spec}
                next_token = data.get("nextPageToken") if gemini else None
                if not next_token:
                    break
                if next_token == page_token:
                    raise ValueError("服务商重复返回同一分页标记，请稍后再获取模型列表")
                page_token = next_token
            else:
                warning = "模型列表超过五页，当前显示已取得的型号；也可手动填写其他型号。"
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise ValueError("获取模型列表超时，请核对网络、代理或服务状态；没有发起模型推理") from exc
    except httpx.RequestError as exc:
        raise ValueError("无法连接模型列表接口，请核对API地址和网络；没有发起模型推理") from exc
    except (TypeError, KeyError) as exc:
        raise ValueError("服务返回的模型列表格式无法识别，可手动填写型号") from exc
    if not found:
        raise ValueError("当前账号未返回可选的对话模型，请确认登录或API Key；也可手动填写型号")
    return {"models": sorted(found.values(), key=lambda row: row["name"].casefold()), "warning": warning, "inference": False}
