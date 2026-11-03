"""Explicit tool catalog; execution, delivery and accounting remain in the core."""
from __future__ import annotations

from dataclasses import dataclass
import inspect


@dataclass
class Tool:
    name: str
    category: str
    description: str
    parameters: dict
    handler: object
    resource: str = "cpu"
    shared: bool = False
    read_only: bool = False

    def schema(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


class Registry:
    def __init__(self):
        self.items = {}

    def add(self, name, category, description, properties, required, handler, resource="cpu", shared=False):
        safe_reads = {"web_search", "fetch_url", "native_fetch", "find_files", "read_document", "search_documents", "read_history", "search_memory", "view_image", "read_skill", "list_mcp_tools", "inspect_artifact"}
        properties = dict(properties)
        if name in {"run_python", "run_shell", "create_document", "create_image", "transform_images", "pdf_operation", "translate_pdf", "native_image_generation", "native_code_interpreter", "native_shell"}:
            properties["change_summary"] = {"type": "string", "description": "修改已有成果时，简述实际实施的内容/结构/排版变化，用于新版说明；不要虚构未执行的修改。"}
        self.items[name] = Tool(name, category, description, {"type": "object", "properties": properties, "required": required, "additionalProperties": False}, handler, resource, shared, name in safe_reads)

    def schemas(self, categories):
        return [t.schema() for t in self.items.values() if t.category in categories]

    async def call(self, name, args, message, task_id):
        import jsonschema
        tool = self.items.get(name)
        if not tool:
            raise ValueError("工具不存在或尚未加载")
        jsonschema.validate(args, tool.parameters)
        values = dict(args)
        summary = values.pop("change_summary", "") if "change_summary" in tool.parameters["properties"] else ""
        result = tool.handler(message, task_id, **values)
        result = await result if inspect.isawaitable(result) else result
        if summary:
            result.data["change_summary"] = summary
        return result


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
OBJECT = {"type": "object"}
