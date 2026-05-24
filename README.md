# Game Update Tracker

Typed Python script for tracking Steam game changelog/news updates by app ID and alerting when new entries appear.

It uses Steam's official `ISteamNews/GetNewsForApp` endpoint. A Steam store news URL such as `https://store.steampowered.com/news/app/2483190?updates=true` corresponds to app ID `2483190` in this tracker.

## Setup

1. Copy `.env.example` to `.env`.
2. Set `STEAM_GAMES` with the Steam app IDs and display names you want to track.
3. Run:

```bash
python track_game_updates.py --env .env
```

The first run records the latest known update without alerting by default. Set `ALERT_ON_FIRST_RUN=true` if you want existing entries to alert immediately.

## Polling

Run continuously with an interval in seconds:

```bash
python track_game_updates.py --env .env --interval 1800
```

For production use, prefer running it as a scheduled task or cron job without `--interval`.

## GitHub Actions

This repository includes `.github/workflows/track-game-updates.yml`, which runs once per day at `10:00 UTC` and can also be started manually from the GitHub Actions tab.

Add these GitHub repository variables:

- `STEAM_GAMES`: example `2483190:Example Game`
- `NEWS_COUNT`: optional, defaults to `10`
- `ALERT_ON_FIRST_RUN`: optional, defaults to `false`
- `REQUEST_TIMEOUT_SECONDS`: optional, defaults to `20`

Add this GitHub repository secret if you want Discord alerts:

- `DISCORD_WEBHOOK_URL`

The workflow commits `.game_changelog_state.json` back to the repository after each run so it can detect only new updates next time.

## Alerts

Alerts are printed to stdout. To also send Discord alerts, set `DISCORD_WEBHOOK_URL` in `.env`.

## Configuration

`STEAM_GAMES` uses this format:

```env
STEAM_GAMES=2483190:Example Game
```

You can also use `STEAM_APP_IDS` plus `GAME_NAME_<app_id>` variables if that is easier for automation.

SteamDB is useful for manual research, but this script intentionally avoids depending on SteamDB because there is no stable public changelog API intended for polling.
