#!/usr/bin/env python3
"""Track Steam game changelog/news updates and alert when new entries appear."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict


STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"


class NewsItem(TypedDict, total=False):
    gid: str
    title: str
    url: str
    date: int
    contents: str


class GameState(TypedDict, total=False):
    last_seen_gid: str
    last_seen_date: int


class StateFile(TypedDict):
    games: dict[str, GameState]


@dataclass(frozen=True)
class GameTarget:
    app_id: str
    name: str


@dataclass(frozen=True)
class Config:
    games: list[GameTarget]
    state_file: Path
    news_count: int
    request_timeout_seconds: float
    discord_webhook_url: str | None
    alert_on_first_run: bool


@dataclass(frozen=True)
class Alert:
    game: GameTarget
    item: NewsItem


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_games() -> list[GameTarget]:
    games: list[GameTarget] = []
    configured_games = os.environ.get("STEAM_GAMES", "").strip()

    if configured_games:
        for entry in configured_games.split(","):
            entry = entry.strip()
            if not entry:
                continue
            app_id, separator, name = entry.partition(":")
            app_id = app_id.strip()
            if not app_id.isdigit():
                raise ValueError(f"Invalid Steam app id in STEAM_GAMES: {app_id!r}")
            games.append(GameTarget(app_id=app_id, name=name.strip() if separator else app_id))

    app_ids = os.environ.get("STEAM_APP_IDS", "").strip()
    if app_ids:
        known_ids = {game.app_id for game in games}
        for app_id in app_ids.split(","):
            app_id = app_id.strip()
            if not app_id:
                continue
            if not app_id.isdigit():
                raise ValueError(f"Invalid Steam app id in STEAM_APP_IDS: {app_id!r}")
            if app_id not in known_ids:
                games.append(GameTarget(app_id=app_id, name=os.environ.get(f"GAME_NAME_{app_id}", app_id)))

    if not games:
        raise ValueError("Set STEAM_GAMES or STEAM_APP_IDS in the environment file.")

    return games


def load_config(env_path: Path) -> Config:
    load_env_file(env_path)
    return Config(
        games=parse_games(),
        state_file=Path(os.environ.get("STATE_FILE", ".game_changelog_state.json")),
        news_count=int(os.environ.get("NEWS_COUNT", "10")),
        request_timeout_seconds=float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "20")),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or None,
        alert_on_first_run=parse_bool(os.environ.get("ALERT_ON_FIRST_RUN"), default=False),
    )


def load_state(path: Path) -> StateFile:
    if not path.exists():
        return {"games": {}}
    with path.open("r", encoding="utf-8") as state_file:
        data = json.load(state_file)
    if not isinstance(data, dict) or not isinstance(data.get("games"), dict):
        raise ValueError(f"Invalid state file format: {path}")
    return data  # type: ignore[return-value]


def save_state(path: Path, state: StateFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def request_json(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "game-update-track/1.0"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object response")
    return data


def fetch_news(game: GameTarget, count: int, timeout_seconds: float) -> list[NewsItem]:
    query = urllib.parse.urlencode(
        {
            "appid": game.app_id,
            "count": count,
            "maxlength": 0,
            "format": "json",
        }
    )
    data = request_json(f"{STEAM_NEWS_URL}?{query}", timeout_seconds)
    app_news = data.get("appnews")
    if not isinstance(app_news, dict):
        return []
    news_items = app_news.get("newsitems", [])
    if not isinstance(news_items, list):
        return []
    return [item for item in news_items if isinstance(item, dict)]  # type: ignore[list-item]


def strip_markup(value: str, limit: int = 280) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    without_bbcode = re.sub(r"\[[^\]]+\]", " ", without_tags)
    collapsed = re.sub(r"\s+", " ", html.unescape(without_bbcode)).strip()
    return collapsed[: limit - 1] + "..." if len(collapsed) > limit else collapsed


def format_timestamp(timestamp: int | None) -> str:
    if not timestamp:
        return "unknown date"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def find_new_items(items: list[NewsItem], previous: GameState | None, alert_on_first_run: bool) -> list[NewsItem]:
    if not items:
        return []
    if previous is None or not previous.get("last_seen_gid"):
        return list(reversed(items)) if alert_on_first_run else []

    last_seen_gid = previous.get("last_seen_gid")
    last_seen_date = int(previous.get("last_seen_date", 0))
    newest_first: list[NewsItem] = []

    for item in items:
        if item.get("gid") == last_seen_gid:
            break
        if int(item.get("date", 0)) > last_seen_date or last_seen_gid not in {candidate.get("gid") for candidate in items}:
            newest_first.append(item)

    return list(reversed(newest_first))


def update_marker(state: StateFile, game: GameTarget, items: list[NewsItem]) -> None:
    if not items:
        return
    latest = items[0]
    state["games"][game.app_id] = {
        "last_seen_gid": str(latest.get("gid", "")),
        "last_seen_date": int(latest.get("date", 0)),
    }


def print_alert(alert: Alert) -> None:
    title = alert.item.get("title", "Untitled update")
    url = alert.item.get("url", "")
    date = format_timestamp(alert.item.get("date"))
    summary = strip_markup(alert.item.get("contents", ""))
    print(f"[{alert.game.name}] {title}")
    print(f"Date: {date}")
    if url:
        print(f"URL: {url}")
    if summary:
        print(f"Summary: {summary}")
    print()


def post_discord_alert(webhook_url: str, alert: Alert, timeout_seconds: float) -> None:
    title = alert.item.get("title", "Untitled update")
    url = alert.item.get("url", "")
    date = format_timestamp(alert.item.get("date"))
    summary = strip_markup(alert.item.get("contents", ""), limit=900)
    content = f"**{alert.game.name} update:** {title}\n{date}"
    if url:
        content += f"\n{url}"
    if summary:
        content += f"\n\n{summary}"

    payload = json.dumps({"content": content[:1900]}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "game-update-track/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response.read()


def check_once(config: Config) -> int:
    state = load_state(config.state_file)
    alerts: list[Alert] = []

    for game in config.games:
        items = fetch_news(game, config.news_count, config.request_timeout_seconds)
        previous = state["games"].get(game.app_id)
        for item in find_new_items(items, previous, config.alert_on_first_run):
            alerts.append(Alert(game=game, item=item))
        update_marker(state, game, items)

    save_state(config.state_file, state)

    if not alerts:
        print("No new game updates found.")
        return 0

    for alert in alerts:
        print_alert(alert)
        if config.discord_webhook_url:
            post_discord_alert(config.discord_webhook_url, alert, config.request_timeout_seconds)

    return len(alerts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track Steam game changelog/news updates.")
    parser.add_argument("--env", default=".env", help="Path to environment file. Default: .env")
    parser.add_argument("--interval", type=int, default=0, help="Poll every N seconds. Default: run once")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config = load_config(Path(args.env))

    while True:
        try:
            check_once(config)
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as error:
            print(f"Error: {error}", file=sys.stderr)
            if not args.interval:
                return 1

        if not args.interval:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
