"""Reuse image uploads by provider endpoint, credential and content, never by filename."""
import asyncio
import base64
import copy
import hashlib

import httpx

from .native_capabilities import family
from .domain import CapabilityUnavailable


def cache_key(models, provider, raw):
    identity = provider["base_url"].rstrip("/")+":"+models.config.provider_key(provider)
    return "provider-image-v2:"+hashlib.sha256(identity.encode()+raw).hexdigest()


def invalidate_images(models, provider, messages):
    for message in messages:
        if not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            url = part.get("image_url", {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                raw = base64.b64decode(url.split(";base64,", 1)[1], validate=True)
                models.store.execute("DELETE FROM cache WHERE key=?", (cache_key(models, provider, raw),))


async def reuse_provider_images(models, provider, messages):
    native = models.config.get("native")
    service = family(provider)
    if service not in {"deepseek", "openai"} or not native["enabled"] or not native["features"].get("image_reuse", True):
        return messages
    if service == "openai" and provider.get("kind") != "responses" and not provider.get("model", "").startswith(("gpt-6", "gpt-5.4", "gpt-5.5")):
        return messages
    key = models.config.provider_key(provider)
    if not key:
        raise CapabilityUnavailable("图片输入缺少当前服务商密钥")
    result = copy.deepcopy(messages)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15)) as client:
        for message in result:
            if message.get("role") != "user" or not isinstance(message.get("content"), list):
                continue
            for index, part in enumerate(message["content"]):
                url = part.get("image_url", {}).get("url", "") if part.get("type") == "image_url" else ""
                if not url.startswith("data:") or ";base64," not in url:
                    continue
                mime, encoded = url[5:].split(";base64,", 1)
                raw = base64.b64decode(encoded, validate=True)
                ident = cache_key(models, provider, raw)
                # A bounded set of locks; cache persists independently of the lock lifetime.
                lock = models.image_upload_locks.setdefault(ident[-2:], asyncio.Lock())
                async with lock:
                    saved = models.store.cached(ident)
                    if not saved:
                        endpoint = "https://api.deepseek.com/files" if service == "deepseek" else provider["base_url"].rstrip("/")+"/files"
                        response = await client.post(endpoint, headers={"Authorization": "Bearer "+key},
                            data={"purpose": "user_data", "expires_after[anchor]": "created_at", "expires_after[seconds]": "86400"}, files={"file": ("image.png", raw, mime)})
                        if response.status_code in {400, 404, 415}:
                            # Files API unsupported is not proof that image understanding is unavailable.
                            models.store.event("reuse", "图片上传复用不可用，本次使用直接图片输入", {"provider": provider["id"], "status": response.status_code})
                            continue
                        models.raise_error(response)
                        saved = {"id": response.json()["id"]}
                        models.store.cached(ident, saved, 23*3600)
                    message["content"][index] = {"type": "file", "file_id": saved["id"]}
    return result
