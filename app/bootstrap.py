"""Application bootstrap helpers.

In Phase 0 this module documents the target boot flow. Runtime still delegates to
app.legacy_bot.main. In the next phases, routers will be registered here instead
of inside the legacy file.
"""

from __future__ import annotations

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage


def create_dispatcher(*routers: Router) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    for router in routers:
        dp.include_router(router)
    return dp
