# claude-scheduler

A [Herdr app](https://herdr.dev) mod and a small background service for
Claude Code's usage limits:

- **Window kicker.** Claude's 5-hour usage window starts with the first
  request after the previous one ends. The kicker sends a minimal request
  (`claude -p` with Haiku) at a start time you pick for each weekday, then
  again a minute after each window ends, until that day's end time. The next
  window opens on time even when you are away.
- **Limit resume.** When a session stops because it hit a usage limit, the
  service waits until a few minutes after the reset and sends that session a
  message (`hc send`), so it carries on by itself.

## How it works

`mod/claude-scheduler/claude_scheduler.py` is both the service and the
command line the popup talks to. It runs as a systemd user unit
(`claude-scheduler.service`) on each machine, so it keeps working while the
Herdr window is closed. It uses only the Python standard library.

The popup (`ctrl-shift-s` once bound) shows one tab per Herdr machine. For
each one it reads `claude_scheduler.py status` and saves with
`claude_scheduler.py config set`, run on that machine through herdr-core.
Nothing takes effect until you press **Save**.

### Kicker

- One row per weekday: on or off, a start time, and an optional end time.
  With no end time, a run lasts until the next scheduled start.
- Runs may cross midnight, and runs that overlap merge into one.
- A kick that lands before the end time goes ahead even if its window runs
  past it. The end time only stops the kicker.
- The next kick is timed from the window's real end, which each kick reports,
  so a window you opened yourself is never cut short and sleep doesn't delay
  the schedule (it kicks on wake).
- Usage windows belong to your account, not to a machine, so one machine is
  enough. Saving the kicker on one machine turns it off on the others. If
  another machine can't be reached, it is still saved, with a warning: both
  may kick for a while, which is harmless.

### Resume

- On or off, with an optional date and time when it turns itself off. When
  it does, you get a notification.
- An optional list of session names (the names `/rename` sets). Empty means
  every session. The popup offers the running sessions and takes typed names.
- The message it sends is editable.
- A limit stop is recognised from the session transcript, and the session's
  herdr terminal from its process.
- If the session's input box holds text you haven't sent, it is skipped and
  you are notified: sending would glue the message onto your text and submit
  both. It is resumed on a later pass once you send or clear it.
- Sessions that are not running in a herdr terminal can't be resumed.

### Notifications

The service can't post Herdr notifications while the window is closed. It
uses `notify-send` where there is one, and the popup shows anything you
missed as notifications the next time it opens, plus a list of recent events.

## Install

Requirements on each machine: Linux with systemd user services, Python 3.10+,
Claude Code (`claude` on `PATH` or in `~/.local/bin`) and herdr-core (`hc`).

```sh
git clone https://github.com/LeoKenny/claude-scheduler.git
cd claude-scheduler
./install.sh
```

That copies the mod into `~/.config/herdr-app/mods/claude-scheduler` and
installs the service on this machine. For another Herdr machine, open the
popup, pick its tab and press **Install service**.

Bind the popup to a key in `~/.config/herdr-app/keys.json`, in `global`:

```json
"ctrl-shift-s": "mod.open:claude-scheduler"
```

## Files

| What | Where |
|---|---|
| Settings | `$XDG_CONFIG_HOME/claude-scheduler/config.json` (by hand is fine: a bad edit keeps the last good settings and shows the error) |
| State, events | `$XDG_STATE_HOME/claude-scheduler/` |
| Installed service | `$XDG_DATA_HOME/claude-scheduler/claude_scheduler.py` |
| Unit | `~/.config/systemd/user/claude-scheduler.service` |
| Log | `journalctl --user -u claude-scheduler` |

## Command line

```sh
claude_scheduler.py status          # JSON
claude_scheduler.py config get
claude_scheduler.py config set '{"kicker": {...}, "resume": {...}}'
claude_scheduler.py kicker on|off
claude_scheduler.py install | uninstall
```

Settings shape:

```json
{
  "kicker": {
    "enabled": true,
    "days": { "mon": { "enabled": true, "start": "06:00", "end": "18:00" }, "...": {} }
  },
  "resume": {
    "enabled": true,
    "until": "2026-12-01 08:00",
    "sessions": ["my-session"],
    "message": "I hit my usage limit while you were working, but it has reset now. Please continue from where you left off."
  }
}
```

## Uninstall

```sh
python3 ~/.local/share/claude-scheduler/claude_scheduler.py uninstall
rm -rf ~/.config/herdr-app/mods/claude-scheduler
```

Then remove the key from `keys.json`. Settings and state stay until you
delete `~/.config/claude-scheduler` and `~/.local/state/claude-scheduler`.

## Tests

```sh
python3 -m unittest discover -s tests
```

## License

MIT
