"""Bundled recipes: ready-made plugins run by ``busymeter run <recipe>`` (see ../../../recipes/README.md).

Only the names in RECIPES can be run, installed or configured (the MCP server takes nothing else: no paths, no code).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Recipe

# name -> (module, class)
RECIPES: dict[str, tuple[str, str]] = {
    "stream_status": ("busymeter.recipes.stream_status", "StreamStatus"),
    "game_state": ("busymeter.recipes.game_state", "GameState"),
    "focus_timer": ("busymeter.recipes.focus_timer", "FocusTimer"),
    "vibe_meter": ("busymeter.recipes.vibe_meter", "VibeMeter"),
}


def names() -> list[str]:
    return list(RECIPES)


def get(name: str) -> "type[Recipe]":
    """The recipe class for a bundled name. KeyError for anything else (never a path or a module name)."""
    if not isinstance(name, str) or name not in RECIPES:
        raise KeyError(name)
    module, cls = RECIPES[name]
    return getattr(importlib.import_module(module), cls)
