import sys
import types


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Filter:
    def command(self, _name):
        return lambda func: func


class _Star:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config or {}


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
event = types.ModuleType("astrbot.api.event")
star = types.ModuleType("astrbot.api.star")

api.logger = _Logger()
event.AstrMessageEvent = object
event.filter = _Filter()
star.Context = object
star.Star = _Star
astrbot.api = api

sys.modules.update(
    {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
    }
)

