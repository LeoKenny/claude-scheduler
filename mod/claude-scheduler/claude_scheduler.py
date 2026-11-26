#!/usr/bin/env python3
"""Keep Claude Code's 5-hour usage windows open on a weekly schedule, and
resume sessions that a usage limit stopped.

Two jobs, both read from one settings file:

* The kicker. On each enabled weekday, from its start time to its end time,
  it sends a minimal request that opens a fresh 5-hour window, then another
  one a minute after each window ends. A run with no end time lasts until the
  next scheduled start. A kick that lands before the end goes ahead even if
  its window runs past it.
* Resume. When a session's last turn is the "You've hit your limit" message,
  it waits until a few minutes after the reset and sends the session a
  message through herdr-core (`hc send`). A session with unsent text in its
  input box is skipped, not typed over. It can be limited to sessions by name
  and switched off at a set date and time.

  claude_scheduler.py run             the service loop (systemd user unit)
  claude_scheduler.py status          what it is doing, as JSON
  claude_scheduler.py config get      the settings, as JSON
  claude_scheduler.py config set J    replace the settings with JSON J
  claude_scheduler.py kicker on|off   switch the kicker only
  claude_scheduler.py install         copy itself, add and start the unit
  claude_scheduler.py uninstall       stop and remove the unit (keeps settings)

Paths follow XDG: settings in $XDG_CONFIG_HOME/claude-scheduler, state and
events in $XDG_STATE_HOME/claude-scheduler, the installed copy in
$XDG_DATA_HOME/claude-scheduler. Standard library only.
"""
import datetime as dt
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import time

VERSION = "0.1.1"
NAME = "claude-scheduler"
HOME = os.path.expanduser("~")


def xdg(var, default):
    return os.path.join(os.environ.get(var) or os.path.join(HOME, default), NAME)


CONFIG_DIR = xdg("XDG_CONFIG_HOME", ".config")
STATE_DIR = xdg("XDG_STATE_HOME", ".local/state")
DATA_DIR = xdg("XDG_DATA_HOME", ".local/share")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
STATE = os.path.join(STATE_DIR, "state.json")
EVENTS = os.path.join(STATE_DIR, "events.jsonl")
UNIT_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "systemd", "user")
UNIT = os.path.join(UNIT_DIR, f"{NAME}.service")
SESSIONS = os.path.join(HOME, ".claude", "sessions")
PROJECTS = os.path.join(HOME, ".claude", "projects")

TICK = 30          # seconds between passes
KICK_BUFFER = 60   # seconds past a window's end before the next kick
RESUME_BUFFER = 180  # seconds past a reset; earlier requests can still be rejected
NO_INFO_RETRY = 5 * 3600
FAIL_RETRY = 600
MAX_EVENTS = 500
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_MESSAGE = ("I hit my usage limit while you were working, but it has reset now. "
                   "Please continue from where you left off.")

# Extra places a user service may not have on PATH.
os.environ["PATH"] = os.pathsep.join(
    [os.environ.get("PATH", ""), os.path.join(HOME, ".local", "bin"), "/usr/local/bin", "/usr/bin"])


def default_config():
    return {
        "kicker": {"enabled": False,
                   "days": {d: {"enabled": False, "start": "06:00", "end": None} for d in DAYS}},
        "resume": {"enabled": False, "until": None, "sessions": [], "message": DEFAULT_MESSAGE},
    }


# --- settings -------------------------------------------------------------

def parse_hhmm(s):
    """'06:00' -> datetime.time; raises ValueError."""
    h, m = s.split(":")
    if len(h) not in (1, 2) or len(m) != 2:
        raise ValueError
    return dt.time(int(h), int(m))


def parse_until(s):
    """'2026-11-30 08:00' (or with a T) -> naive local datetime; raises ValueError."""
    return dt.datetime.strptime(s.strip().replace("T", " "), "%Y-%m-%d %H:%M")


def validate(cfg):
    """A complete, checked copy of cfg, or ValueError naming the first problem."""
    if not isinstance(cfg, dict):
        raise ValueError("settings must be a JSON object")
    out = default_config()
    k = cfg.get("kicker", {})
    if not isinstance(k, dict):
        raise ValueError("kicker must be an object")
    out["kicker"]["enabled"] = bool(k.get("enabled", False))
    days = k.get("days", {})
    if not isinstance(days, dict):
        raise ValueError("kicker.days must be an object")
    for d in DAYS:
        day = days.get(d, {})
        if not isinstance(day, dict):
            raise ValueError(f"kicker.days.{d} must be an object")
        row = out["kicker"]["days"][d]
        row["enabled"] = bool(day.get("enabled", False))
        start = day.get("start") or "06:00"
        try:
            parse_hhmm(start)
        except (ValueError, AttributeError):
            raise ValueError(f"{d}: start {start!r} is not a time like 06:00") from None
        row["start"] = start
        end = day.get("end") or None
        if end is not None:
            try:
                parse_hhmm(end)
            except (ValueError, AttributeError):
                raise ValueError(f"{d}: end {end!r} is not a time like 18:00") from None
            if end == start:
                raise ValueError(f"{d}: end is the same as start")
        row["end"] = end
    r = cfg.get("resume", {})
    if not isinstance(r, dict):
        raise ValueError("resume must be an object")
    out["resume"]["enabled"] = bool(r.get("enabled", False))
    until = r.get("until") or None
    if until is not None:
        try:
            parse_until(until)
        except (ValueError, AttributeError):
            raise ValueError(f"resume.until {until!r} is not a date like 2026-12-01 08:00") from None
    out["resume"]["until"] = until
    sessions = r.get("sessions", [])
    if not isinstance(sessions, list) or not all(isinstance(x, str) for x in sessions):
        raise ValueError("resume.sessions must be a list of names")
    out["resume"]["sessions"] = sorted({x.strip() for x in sessions if x.strip()})
    msg = r.get("message") or DEFAULT_MESSAGE
    if not isinstance(msg, str):
        raise ValueError("resume.message must be text")
    out["resume"]["message"] = msg.strip() or DEFAULT_MESSAGE
    return out


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(value, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def load_config(last_good=None):
    """(settings, error). A missing file is the defaults; a bad one keeps last_good."""
    try:
        with open(CONFIG) as f:
            text = f.read()
    except FileNotFoundError:
        return default_config(), None
    except OSError as e:
        return last_good or default_config(), str(e)
    try:
        return validate(json.loads(text)), None
    except ValueError as e:
        return last_good or default_config(), f"{CONFIG}: {e}"


# --- events ---------------------------------------------------------------

def event(kind, text, notify=False):
    os.makedirs(STATE_DIR, exist_ok=True)
    rec = {"at": int(time.time()), "kind": kind, "text": text, "notify": notify}
    with open(EVENTS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"{time.strftime('%F %T')} {kind}: {text}", flush=True)
    if notify and shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Claude scheduler", "Claude scheduler", text],
                       capture_output=True, timeout=10)


def recent_events(n):
    try:
        with open(EVENTS) as f:
            lines = f.readlines()
    except OSError:
        return []
    if len(lines) > MAX_EVENTS * 2:  # keep the file small
        with open(EVENTS, "w") as f:
            f.writelines(lines[-MAX_EVENTS:])
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# --- schedule -------------------------------------------------------------

def runs(cfg, now):
    """Kick runs as (start, end) naive local datetimes, from a week before now
    to a day after. A run with no end lasts until the next enabled start."""
    days = cfg["kicker"]["days"]
    starts = []
    for i in range(-8, 9):
        day = (now + dt.timedelta(days=i)).date()
        row = days[DAYS[day.weekday()]]
        if row["enabled"]:
            starts.append((dt.datetime.combine(day, parse_hhmm(row["start"])), row["end"]))
    starts.sort()
    out = []
    for j, (start, end) in enumerate(starts):
        if end is not None:
            stop = dt.datetime.combine(start.date(), parse_hhmm(end))
            if stop <= start:
                stop += dt.timedelta(days=1)
        else:
            later = [s for s, _ in starts[j + 1:]]
            stop = later[0] if later else start + dt.timedelta(days=7)
        out.append((start, stop))
    return out


def current_run(cfg, now):
    """Start of the latest run that covers now, or None outside every run."""
    covering = [s for s, e in runs(cfg, now) if s <= now < e]
    return max(covering) if covering else None


def next_start(cfg, now):
    later = [s for s, _ in runs(cfg, now) if s > now]
    return min(later) if later else None


def epoch(t):
    return int(t.timestamp())


# --- the kicker -----------------------------------------------------------

def rate_limit(lines):
    """(reset epoch or None, info dict) from `claude -p --output-format stream-json` lines.
    When rejected, the binding limit (5h or weekly) says when requests work
    again; otherwise the 5-hour window's end."""
    info = None
    reply = ""
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
        elif e.get("type") == "result":
            reply = str(e.get("result") or "")
    if not info:
        return None, {}, reply
    windows = info.get("unifiedWindows") or {}
    if info.get("status") == "rejected":
        reset = info.get("resetsAt")
    else:
        reset = (windows.get("five_hour") or {}).get("resetsAt")
        if reset is None and info.get("rateLimitType") == "five_hour":
            reset = info.get("resetsAt")
    return (int(reset) if reset else None), info, reply


def kick(st):
    claude = shutil.which("claude")
    if not claude:
        return False, None, "claude is not on PATH"
    try:
        # No transcript: a kick every few hours would otherwise pile them up.
        p = subprocess.run([claude, "-p", "Reply with only: ok", "--model", "haiku",
                            "--output-format", "stream-json", "--verbose", "--no-session-persistence"],
                           capture_output=True, text=True, timeout=180, cwd=HOME)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, None, str(e)
    reset, info, reply = rate_limit(p.stdout.splitlines())
    windows = info.get("unifiedWindows") or {}
    k = st["kicker"]
    for name in ("five_hour", "seven_day"):
        w = windows.get(name)
        if w:
            k[name] = {"utilization": w.get("utilization"), "resetsAt": w.get("resetsAt")}
    ok = p.returncode == 0 and info.get("status") != "rejected"
    msg = reply.strip() or (p.stderr or p.stdout).strip()[-300:]
    return ok, reset, msg


def kicker_tick(cfg, st, now_dt):
    k = st["kicker"]
    if not cfg["kicker"]["enabled"]:
        return
    run_start = current_run(cfg, now_dt)
    if run_start is None:
        return
    now = epoch(now_dt)
    new_run = k.get("last_kick", 0) < epoch(run_start)
    if not new_run and now < k.get("next_kick", 0):
        return
    ok, reset, msg = kick(st)
    k["last_kick"] = now
    if ok:
        k["window_ends"] = reset
        nxt = reset + KICK_BUFFER if reset else now + NO_INFO_RETRY
        ends = time.strftime("%a %H:%M", time.localtime(reset)) if reset else "unknown"
        event("kick", f"window open until {ends}")
    else:
        nxt = reset + KICK_BUFFER if reset else now + FAIL_RETRY
        event("kick-failed", f"{msg or 'no reply'}; next try {time.strftime('%a %H:%M', time.localtime(nxt))}")
    k["next_kick"] = max(nxt, now + 60)


# --- resume ---------------------------------------------------------------

def pid_alive(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"claude" in f.read()
    except OSError:
        return False


def live_sessions():
    out = []
    for path in glob.glob(os.path.join(SESSIONS, "*.json")):
        s = read_json(path, None)
        if not isinstance(s, dict) or s.get("kind") != "interactive":
            continue
        if not pid_alive(s.get("pid", 0)):
            continue
        out.append(s)
    return out


def session_name(s):
    return s.get("name") or os.path.basename(s.get("cwd") or "") or s.get("sessionId", "")[:8]


def last_entry(session_id):
    """Last main-thread user/assistant entry of the session's transcript."""
    paths = glob.glob(os.path.join(PROJECTS, "*", f"{session_id}.jsonl"))
    if not paths:
        return None
    with open(paths[0], "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 512 * 1024))
        lines = f.read().decode(errors="ignore").splitlines()
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("isSidechain") or e.get("type") not in ("user", "assistant"):
            continue
        return e
    return None


def limit_hit(entry):
    """resetsAt epoch if entry is the synthetic limit-reached message."""
    if not entry or entry.get("type") != "assistant":
        return None
    q = entry.get("quotaLimits") or {}
    if q.get("status") == "rejected" and q.get("resetsAt"):
        return int(q["resetsAt"])
    return None


def hc(*args, timeout=30):
    try:
        p = subprocess.run(["hc", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout)).strip()


def terminal_for(pid):
    """herdr-core address of the terminal whose program is pid, or None."""
    code, out = hc("ls", "--json")
    if code != 0:
        return None
    try:
        rows = json.loads(out)
    except ValueError:
        return None
    rows = rows if isinstance(rows, list) else rows.get("terminals", [])
    for row in rows:
        if row.get("agent") != "claude-code" or row.get("state") != "live":
            continue
        code, got = hc("get", row["address"], "--json")
        if code != 0:
            continue
        try:
            fg = json.loads(got).get("foreground") or {}
        except ValueError:
            continue
        if fg.get("pid") == pid:
            return row["address"]
    return None


def unsent_text(screen):
    """Text in Claude Code's input box (the last ❯ line), or ''. After a limit
    stop no new suggestion can be generated, so text there is the person's."""
    lines = [l for l in screen.splitlines() if "❯" in l]
    if not lines:
        return ""
    return lines[-1].split("❯", 1)[1].replace(" ", " ").strip()


def terminal_status(address):
    code, out = hc("get", address, "--json")
    if code != 0:
        return None
    try:
        return (json.loads(out).get("status") or {}).get("state")
    except ValueError:
        return None


def resume_tick(cfg, st, now_dt):
    r = cfg["resume"]
    rs = st["resume"]
    if not r["enabled"]:
        return
    if r["until"] and now_dt >= parse_until(r["until"]):
        set_config(lambda c: c["resume"].update(enabled=False))
        event("resume-off", f"Limit resume turned off (shut-off {r['until']} reached)", notify=True)
        return
    wanted = set(r["sessions"])
    now = epoch(now_dt)
    for s in live_sessions():
        name = session_name(s)
        if wanted and name not in wanted:
            continue
        e = last_entry(s["sessionId"])
        reset = limit_hit(e)
        if reset is None:
            continue
        key = f"{s['sessionId']}:{e.get('uuid')}"
        if key in rs["pending"] or key in rs["done"]:
            continue
        rs["pending"][key] = {"sessionId": s["sessionId"], "pid": s["pid"], "name": name, "resetsAt": reset}
        event("limit-hit", f"{name} stopped by the limit; resumes after "
              f"{time.strftime('%a %H:%M', time.localtime(reset + RESUME_BUFFER))}")
    for key, p in list(rs["pending"].items()):
        if now < p["resetsAt"] + RESUME_BUFFER:
            continue
        outcome = resume_one(p, key, r["message"], rs)
        if outcome == "retry":
            continue
        del rs["pending"][key]
        rs["done"].append(key)
        rs["skipped"].pop(key, None)
    rs["done"] = rs["done"][-200:]


def resume_one(p, key, message, rs):
    """'done' when handled or dropped, 'retry' to try again on a later pass."""
    name = p["name"]
    if not pid_alive(p["pid"]):
        event("dropped", f"{name}: its Claude process has exited")
        return "done"
    e = last_entry(p["sessionId"])
    if e is None or f"{p['sessionId']}:{e.get('uuid')}" != key:
        event("dropped", f"{name}: already continued")
        return "done"
    address = terminal_for(p["pid"])
    if not address:
        if not rs["skipped"].get(key):
            rs["skipped"][key] = "no-terminal"
            event("skipped", f"{name}: not in a herdr terminal, can't resume it", notify=True)
        return "retry"
    if terminal_status(address) == "working":
        return "retry"
    code, screen = hc("read", address)
    if code != 0:
        return "retry"
    typed = unsent_text(screen)
    if typed:
        if rs["skipped"].get(key) != "draft":
            rs["skipped"][key] = "draft"
            event("skipped", f"{name}: has unsent text, not resumed; send or clear it and it will be",
                  notify=True)
        return "retry"
    code, out = hc("send", address, message)
    if code != 0:
        event("resume-failed", f"{name}: {out}")
        return "retry"
    event("resumed", f"{name} resumed")
    return "done"


# --- state and the loop -----------------------------------------------------

def load_state():
    st = read_json(STATE, {})
    st.setdefault("kicker", {})
    r = st.setdefault("resume", {})
    r.setdefault("pending", {})
    r.setdefault("done", [])
    r.setdefault("skipped", {})
    return st


def set_config(change):
    cfg, err = load_config()
    if err:
        raise ValueError(err)
    change(cfg)
    write_json(CONFIG, validate(cfg))


def loop():
    os.makedirs(STATE_DIR, exist_ok=True)
    cfg, last_err = None, None
    event("started", f"service {VERSION} started")
    while True:
        cfg, err = load_config(cfg)
        if err != last_err:
            if err:
                event("config-error", f"{err}; keeping the last good settings", notify=True)
            last_err = err
        st = load_state()
        st["config_error"] = err
        try:
            now = dt.datetime.now().replace(microsecond=0)
            kicker_tick(cfg, st, now)
            resume_tick(cfg, st, now)
        except Exception as ex:  # keep running on odd transcripts or settings
            event("error", repr(ex))
        st["heartbeat"] = int(time.time())
        write_json(STATE, st)
        time.sleep(TICK)


def service_state():
    if not shutil.which("systemctl"):
        return "no-systemd"
    if not os.path.exists(UNIT):
        return "not-installed"
    p = subprocess.run(["systemctl", "--user", "is-active", f"{NAME}.service"],
                       capture_output=True, text=True)
    return p.stdout.strip() or "unknown"


def status():
    cfg, err = load_config()
    st = load_state()
    now = dt.datetime.now().replace(microsecond=0)
    k = st["kicker"]
    run_start = current_run(cfg, now) if cfg["kicker"]["enabled"] else None
    nxt = next_start(cfg, now) if cfg["kicker"]["enabled"] else None
    sessions = []
    for s in live_sessions():
        sessions.append({"name": session_name(s), "limited": limit_hit(last_entry(s["sessionId"])) is not None})
    return {
        "version": VERSION,
        "host": socket.gethostname(),
        "service": service_state(),
        "heartbeat": st.get("heartbeat"),
        "config": cfg,
        "config_error": err,
        "kicker": {
            "enabled": cfg["kicker"]["enabled"],
            "in_run": run_start is not None,
            "run_started": epoch(run_start) if run_start else None,
            "next_start": epoch(nxt) if nxt else None,
            "next_kick": k.get("next_kick") if run_start else None,
            "window_ends": k.get("window_ends"),
            "five_hour": k.get("five_hour"),
            "seven_day": k.get("seven_day"),
        },
        "resume": {
            "enabled": cfg["resume"]["enabled"],
            "pending": [{"name": p["name"], "at": p["resetsAt"] + RESUME_BUFFER}
                        for p in st["resume"]["pending"].values()],
        },
        "sessions": sorted(sessions, key=lambda x: x["name"].lower()),
        "events": recent_events(12),
    }


# --- install --------------------------------------------------------------

UNIT_TEXT = """[Unit]
Description=Claude scheduler: usage-window kicker and limit resume

[Service]
ExecStart={python} {script} run
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""


def install():
    if not shutil.which("systemctl"):
        raise SystemExit("systemd isn't available here")
    os.makedirs(DATA_DIR, exist_ok=True)
    script = os.path.join(DATA_DIR, "claude_scheduler.py")
    if os.path.abspath(__file__) != script:
        shutil.copyfile(__file__, script)
    os.makedirs(UNIT_DIR, exist_ok=True)
    with open(UNIT, "w") as f:
        f.write(UNIT_TEXT.format(python=sys.executable, script=script))
    for args in (["daemon-reload"], ["enable", f"{NAME}.service"], ["restart", f"{NAME}.service"]):
        subprocess.run(["systemctl", "--user", *args], check=True)
    print(f"installed {script} and started {NAME}.service")


def uninstall():
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{NAME}.service"], capture_output=True)
    for path in (UNIT, os.path.join(DATA_DIR, "claude_scheduler.py")):
        if os.path.exists(path):
            os.remove(path)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print(f"removed {NAME}.service; settings kept in {CONFIG_DIR}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "run":
        loop()
    elif cmd == "status":
        print(json.dumps(status()))
    elif cmd == "config" and argv[2:3] == ["get"]:
        print(json.dumps(load_config()[0]))
    elif cmd == "config" and argv[2:3] == ["set"] and len(argv) == 4:
        try:
            cfg = validate(json.loads(argv[3]))
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        write_json(CONFIG, cfg)
        print(json.dumps(cfg))
    elif cmd == "kicker" and argv[2:3] in (["on"], ["off"]):
        set_config(lambda c: c["kicker"].update(enabled=argv[2] == "on"))
        print(f"kicker {argv[2]}")
    elif cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    else:
        print(__doc__, file=sys.stderr)
        return 0 if cmd in ("-h", "--help") else 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
