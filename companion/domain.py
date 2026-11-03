"""Cross-module value objects; no framework event is persisted."""
from __future__ import annotations

from dataclasses import dataclass, field
import time


@dataclass
class Message:
    id: str
    bot: str
    scope: str
    sender: str
    name: str
    text: str
    private: bool = False
    quote_id: str = ""
    mentioned: bool = False
    topic: str = ""
    attachments: list[dict] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    origin: str = ""
    autonomous: bool = False
    targets: list[str] = field(default_factory=list)
    notify: list[str] = field(default_factory=list)
    batch_ids: list[str] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)
    recent_context: list[dict] = field(default_factory=list)
    context_cursor: int = 0
    reply_to: str = ""
    quote_reply: bool = True
    quote_source: dict = field(default_factory=dict)
    memory_ids: list[str] | None = None
    schedule_id: str = ""
    request_id: str = ""


@dataclass
class ToolResult:
    text: str = ""
    files: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    terminal: bool = False


class QuotaExceeded(RuntimeError):
    pass


class ProviderImageExpired(RuntimeError):
    """A provider explicitly rejected a missing/expired uploaded file, before execution."""
    status_code = 400


class Interrupted(RuntimeError):
    pass


class DeliveryFailed(RuntimeError):
    pass


class CapabilityUnavailable(RuntimeError):
    """Configuration/access failures cannot be fixed by retrying a prompt."""
    pass


class WebFetchFailed(RuntimeError):
    """A public-page read failed; the agent may make one recovery attempt."""
    def __init__(self, url, reason, status_code=None):
        self.url = url
        self.status_code = status_code
        super().__init__(f"网页读取失败：{reason}；地址：{url}")
