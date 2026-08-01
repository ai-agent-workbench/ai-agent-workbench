"""Read CC-Switch model catalog and provide model info for the web app.

The catalog lives at ~/.codex/cc-switch-model-catalog.json and is regenerated
by CC-Switch whenever the active channel changes.  This module caches the parsed
result and refreshes automatically when the file modification time changes.
"""

import json
import os
import time
from pathlib import Path

_CATALOG_PATH = Path.home() / ".codex" / "cc-switch-model-catalog.json"
_cache = {"mtime": 0, "models": [], "default_model": None}


def _load():
    """(Re)load the catalog if the file has changed; return list of model dicts."""
    try:
        st = os.stat(_CATALOG_PATH)
    except OSError:
        _cache["mtime"] = 0
        _cache["models"] = []
        _cache["default_model"] = None
        return []

    if st.st_mtime == _cache["mtime"]:
        return _cache["models"]

    with open(_CATALOG_PATH, encoding="utf-8") as fh:
        data = json.load(fh)

    models = []
    best_priority = -1
    default_model = None

    for raw in data.get("models", []):
        slug = raw.get("slug")
        if not slug:
            continue

        levels = []
        for lv in raw.get("supported_reasoning_levels", []):
            if isinstance(lv, dict):
                levels.append(lv.get("effort", ""))
            else:
                levels.append(str(lv))

        info = {
            "slug": slug,
            "display_name": raw.get("display_name", slug),
            "default_reasoning": raw.get("default_reasoning_level", "medium"),
            "levels": levels or ["low", "medium", "high"],
            "priority": raw.get("priority", 0),
        }
        models.append(info)

        if info["priority"] > best_priority:
            best_priority = info["priority"]
            default_model = slug

    # Sort by priority desc so highest-priority appears first
    models.sort(key=lambda m: m["priority"], reverse=True)

    _cache["mtime"] = st.st_mtime
    _cache["models"] = models
    _cache["default_model"] = default_model
    return models


def get_models():
    """Return the current list of model info dicts (cached)."""
    return _load()


def get_default_model():
    """Return the slug of the highest-priority model, or empty string."""
    _load()
    return _cache["default_model"] or ""


def get_default_reasoning(model_slug):
    """Return the default reasoning level for *model_slug*, or 'medium'."""
    for m in _load():
        if m["slug"] == model_slug:
            return m["default_reasoning"]
    return "medium"


def get_supported_levels(model_slug):
    """Return the list of supported reasoning levels for *model_slug*."""
    for m in _load():
        if m["slug"] == model_slug:
            return m["levels"]
    return ["low", "medium", "high"]


def get_display_name(model_slug):
    """Return the human-readable display name, falling back to slug."""
    for m in _load():
        if m["slug"] == model_slug:
            return m["display_name"]
    return model_slug
