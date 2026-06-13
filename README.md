# Codex Goal Status Notifier

https://github.com/user-attachments/assets/983efc47-19f7-4647-8a47-4ffa4b728f7e

Get a notification when a Codex goal becomes active, paused, blocked, resumed, or complete.

It uses two signals:

- Codex hooks catch goal completion and blocked status.
- A small local watcher catches pause/resume from Codex's local goal database.

You can use both, or turn either one off in config.

```text
Goal status: active -> blocked
Goal status: paused -> active
Goal status: complete
```

## Setup

Copy the script and config:

```sh
mkdir -p ~/.codex/goal-status-hook
cp hooks/goal_status_report.py ~/.codex/goal-status-hook/goal_status_report.py
cp config.example.json ~/.codex/goal-status-hook/config.json
chmod +x ~/.codex/goal-status-hook/goal_status_report.py
```

Edit:

```sh
~/.codex/goal-status-hook/config.json
```

Set your [ntfy](https://ntfy.sh/) topic:

```json
{
  "signals": {
    "post_tool_use_update_goal": true,
    "sqlite_goal_state": true
  },
  "ntfy": {
    "enabled": true,
    "server": "https://ntfy.sh",
    "topic": "YOUR_PRIVATE_TOPIC",
    "detail": "status_only"
  }
}
```

Signal options:

- Both signals: leave both as `true`
- Hooks only: set `"sqlite_goal_state": false`
- Watcher/SQLite only: set `"post_tool_use_update_goal": false`

## Add Codex Hooks

Copy or merge `hooks.json.example` into your Codex hooks config.

For global hooks, use:

```sh
~/.codex/hooks.json
```

Replace this placeholder in the example:

```text
/ABSOLUTE/PATH/TO/codex-goal-hooks
```

with this repo's absolute path.

After changing hooks, trust the hook commands in Codex when prompted (restart the Codex client).

## Start The Watcher

The watcher is recommended because pause/resume may not run a Codex hook.

Install it as a macOS LaunchAgent:

```sh
mkdir -p ~/Library/LaunchAgents
sed "s#__HOME__#$HOME#g" launchd/com.codex.goal-status-hook.plist.example \
  > ~/Library/LaunchAgents/com.codex.goal-status-hook.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.codex.goal-status-hook.plist
launchctl kickstart -k "gui/$(id -u)/com.codex.goal-status-hook"
```

Check it:

```sh
launchctl print "gui/$(id -u)/com.codex.goal-status-hook"
```

Uninstall it:

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.codex.goal-status-hook.plist
rm ~/Library/LaunchAgents/com.codex.goal-status-hook.plist
```

## Logs

Local logs live here:

```sh
~/.codex/goal-status-hook
```

Useful checks:

```sh
tail -n 20 ~/.codex/goal-status-hook/events.jsonl
tail -n 20 ~/.codex/goal-status-hook/errors.log
tail -n 20 ~/.codex/goal-status-hook/runs.jsonl
```

If complete/block notifications work but pause/resume does not, the watcher is probably not running.

## Test

```sh
python3 -m py_compile hooks/goal_status_report.py
python3 -m unittest discover -s tests
```
