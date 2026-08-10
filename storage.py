"""
storage.py — tiny JSON-file "database" for linked creators + tree state.

Data shape (data.json):
{
  "users": {
    "<discord_user_id>": {
      "youtube_username": "..." | null,   # from Discord Connections
      "tiktok_username": "..." | null,    # from Discord Connections
      "tree_level": 0,
      "last_growth_time": "2026-08-11T12:00:00+00:00" | null
    }
  }
}
"""

import json
import os
from datetime import datetime, timezone

import config


def _empty_user():
    return {
        "youtube_username": None,
        "tiktok_username": None,
        "tree_level": 0,
        "last_growth_time": None,
    }


def load():
    if not os.path.exists(config.DATA_FILE):
        return {"users": {}}
    with open(config.DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"users": {}}


def save(data):
    with open(config.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_user(data, discord_id: str):
    if discord_id not in data["users"]:
        data["users"][discord_id] = _empty_user()
    return data["users"][discord_id]


def grow_tree(user_entry: dict) -> int:
    """
    Registers one growth event (one /posted call, for one platform) for
    this creator. Returns the tree's new level.

    Rules:
      - First ever counted post -> level 1
      - Next post within GROWTH_WINDOW_HOURS of the last counted post
        -> level + 1 (capped at MAX_TREE_LEVEL)
      - Next post AFTER GROWTH_WINDOW_HOURS has passed -> resets to level 1
      - Two growth events processed back-to-back (e.g. /posted youtube
        then /posted tiktok the same day) each count separately, so
        together they add +2.
    """
    now = datetime.now(timezone.utc)
    last_iso = user_entry.get("last_growth_time")

    if last_iso:
        last_dt = datetime.fromisoformat(last_iso)
        hours_passed = (now - last_dt).total_seconds() / 3600.0
        if hours_passed > config.GROWTH_WINDOW_HOURS:
            user_entry["tree_level"] = 1
        else:
            user_entry["tree_level"] = min(
                user_entry["tree_level"] + 1, config.MAX_TREE_LEVEL
            )
    else:
        user_entry["tree_level"] = 1

    user_entry["last_growth_time"] = now.isoformat()
    return user_entry["tree_level"]


def check_and_reset_if_stale(user_entry: dict) -> bool:
    """
    Call this whenever a tree's status is displayed (e.g. /tree) so a
    tree that's gone quiet visibly drops back to level 1 rather than
    just sitting at its old level until the creator posts again.
    Returns True if it reset.
    """
    last_iso = user_entry.get("last_growth_time")
    if not last_iso or user_entry.get("tree_level", 0) == 0:
        return False
    now = datetime.now(timezone.utc)
    last_dt = datetime.fromisoformat(last_iso)
    hours_passed = (now - last_dt).total_seconds() / 3600.0
    if hours_passed > config.GROWTH_WINDOW_HOURS and user_entry["tree_level"] != 1:
        user_entry["tree_level"] = 1
        # Don't touch last_growth_time here — that only advances on an
        # actual new /posted call, so the *next* real post still starts fresh.
        return True
    return False
