from astrbot.api import star
from astrbot.api.event import filter

from companion import runtime


class Main(star.Star):
    def __init__(self, context):
        super().__init__(context)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=25000)
    async def capture(self, event):
        app = runtime.ACTIVE
        if not app or event.get_sender_id() == event.get_self_id():
            return
        if str(event.get_self_id()) != app.config.get("account", "qq"):
            event.stop_event()
            return
        message = await app.astrbot.ingest(event)
        if app.chat.allowed(message):
            # Capture owns persistence before third-party plugins run. Dispatch
            # must not mistake this same event's saved row for a replay.
            if not app.memory.record(message):
                event.stop_event()
                return
            event.set_extra("companion_message", message)
            runtime.SOURCE_EVENT.set(message)
        else:
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-25000)
    async def dispatch(self, event):
        app = runtime.ACTIVE
        message = event.get_extra("companion_message")
        if app and message and not event.is_stopped() and not event.get_result() and not event._has_send_oper:
            await app.chat.ingest(message, recorded=True)
            event.stop_event()
