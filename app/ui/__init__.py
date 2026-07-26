"""Assembles the admin interface into one router tree."""

from __future__ import annotations

from collections.abc import Iterable

from aiogram import F, Router

from . import ai, binding, channels, groups, menu, settings, stats
from .common import OwnerOnly

_root: Router | None = None
_gate: OwnerOnly | None = None


def build_router(owner_ids: Iterable[int]) -> Router:
    """Returns the router tree, building it on first use.

    The per-feature routers are module-level singletons (the aiogram idiom), and
    a Router may only ever have one parent — so the tree is assembled once and
    reused. A later call just refreshes the owner list rather than raising.
    """
    global _root, _gate

    if _root is not None:
        assert _gate is not None
        _gate.owners = frozenset(owner_ids)
        return _root

    gate = OwnerOnly(owner_ids)
    private = Router(name="private")
    private.message.filter(F.chat.type == "private")
    private.callback_query.filter(F.message.chat.type == "private")
    private.message.middleware(gate)
    private.callback_query.middleware(gate)
    private.include_routers(
        menu.router,
        channels.router,
        groups.router,
        ai.router,
        settings.router,
        stats.router,
    )

    root = Router(name="root")
    root.include_router(private)
    # Group updates stay outside the owner gate: topic discovery has to work
    # regardless of who is talking, and /bind checks the owner itself.
    root.include_router(binding.router)

    _root, _gate = root, gate
    return root
