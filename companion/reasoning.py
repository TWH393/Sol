"""Provider-native reasoning capabilities. UI and request encoding share this contract.

Known-model declarations: official provider documentation, checked 2026-11-01.
See docs/模型思考参数.md. Account catalog declarations take precedence.
"""
from __future__ import annotations

import copy


def capability(provider):
    model = provider.get("model", "").removeprefix("models/").lower()
    declared = provider.get("reasoning") or {}
    if declared.get("model") == model and declared.get("source") in {"catalog", "manual"}:
        if declared.get("kind") == "budget" and provider.get("kind") == "gemini":
            lo, hi, step = (declared.get(k) for k in ("min", "max", "step"))
            if all(type(v) is int for v in (lo, hi, step)) and 0 <= lo <= hi <= 1048576 and step > 0:
                return copy.deepcopy(declared)
        if declared.get("kind") == "levels" and declared.get("values"):
            values = [str(v) for v in declared["values"] if isinstance(v, (str, int)) and len(str(v)) < 32]
            return {"kind": "levels", "values": list(dict.fromkeys(values)), "source": declared["source"], "model": model}
    values = []
    if provider.get("kind") == "gemini":
        if model.startswith(("gemini-3.1-flash-lite-image",)):
            values = ["minimal", "high"]
        elif model.startswith(("gemini-3.1-flash-lite", "gemini-3-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash")):
            values = ["minimal", "low", "medium", "high"]
        elif model.startswith(("gemini-3.1-pro", "gemini-3-pro", "gemini-3.8-flash", "gemini-4-argon")):
            values = ["low", "medium", "high"]
    elif model.startswith(("gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6")):
        values = ["none", "low", "medium", "high", "xhigh", "max"]
    elif model.startswith(("gpt-6.1-sol", "gpt-6-astra")):
        values = ["low", "medium", "high", "xhigh", "max"]
    elif model in {"deepseek-flash", "deepseek-v4-pro"}:
        values = ["none", "low", "high", "max"]
    if values:
        return {"kind": "levels", "values": values, "source": "provider_docs", "model": model}
    values = provider.get("efforts") or []
    if values:
        return {"kind": "levels", "values": [str(v) for v in values], "source": "manual", "model": model}
    return {"kind": "unknown", "values": [], "source": "unavailable", "model": model}


def effective(provider):
    value = copy.deepcopy(provider)
    spec = capability(value)
    value["reasoning"] = spec
    value["efforts"] = spec.get("values", [])
    return value


def validate(provider, value):
    if value in (None, "", "default"):
        return "default"
    spec = capability(provider)
    if spec["kind"] == "budget":
        try:
            n = int(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("该模型的思考预算必须是整数") from exc
        if str(n) != str(value) or not spec["min"] <= n <= spec["max"] or (n-spec["min"]) % spec["step"]:
            raise ValueError("思考预算超出该模型支持的范围或步进")
        return str(n)
    if str(value) not in spec.get("values", []):
        raise ValueError("该模型没有声明支持此思考设置，请重新选择或恢复模型默认")
    return str(value)


def gemini_config(provider, effort):
    value = validate(provider, effort)
    if value == "default":
        return {}
    return {"thinkingBudget": int(value)} if capability(provider)["kind"] == "budget" else {"thinkingLevel": value.upper()}
