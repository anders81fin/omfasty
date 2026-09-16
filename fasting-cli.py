#!/usr/bin/env python3
"""State + history backend for the anko11.fasting Omarchy plugin.

Usage:
  fasting-cli.py start <hours>     start a fast (no-op if already fasting)
  fasting-cli.py target <hours>    change the target of an in-progress fast
  fasting-cli.py stop              end the current fast, log it to history,
                                   and open the eating window
  fasting-cli.py idle              stop whatever is counting WITHOUT logging
                                   anything: discards a running fast, or closes
                                   the eating window
  fasting-cli.py status            print current state + streak + history as JSON
  fasting-cli.py nudge <kind> <hour> <title> <body>
                                   send an hourly nudge, at most once per hour
"""
import json
import os
import subprocess
import sys
import time

STATE_DIR = os.path.expanduser("~/.local/state/omarchy-fasting")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
HISTORY_FILE = os.path.join(STATE_DIR, "history.jsonl")
NUDGE_DIR = os.path.join(STATE_DIR, "nudges")
NUDGE_KEEP_SECONDS = 172800
RECENT_COUNT = 3
RECENT_MIN_HOURS = 12.0
LONGEST_COUNT = 3
DEFAULT_TARGET_HOURS = 16.0


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {
            "fasting": bool(data.get("fasting", False)),
            "startedAt": int(data.get("startedAt", 0)),
            "targetHours": float(data.get("targetHours", DEFAULT_TARGET_HOURS)),
            # Nothing is counting: neither a fast nor the eating window. Absent
            # from state files written before this flag existed, and defaulting
            # to False there is what keeps their eating window running as it
            # did rather than silently stopping on upgrade.
            "idle": bool(data.get("idle", False)),
        }
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return {
            "fasting": False,
            "startedAt": 0,
            "targetHours": DEFAULT_TARGET_HOURS,
            # A first run has no eating window to continue, so it starts idle
            # instead of counting up from a lastEnd it does not have.
            "idle": True,
        }


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def append_history(entry):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    entries = []
    with open(HISTORY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def compute_streak(entries):
    streak = 0
    for entry in reversed(entries):
        if entry.get("actualHours", 0) >= entry.get("targetHours", 0):
            streak += 1
        else:
            break
    return streak


def recent_long_fasts(entries):
    long_enough = [e for e in entries if e.get("actualHours", 0) >= RECENT_MIN_HOURS]
    long_enough.sort(key=lambda e: e.get("end", 0), reverse=True)
    return long_enough[:RECENT_COUNT]


def longest_fasts(entries):
    return sorted(entries, key=lambda e: e.get("actualHours", 0), reverse=True)[:LONGEST_COUNT]


def cmd_start(hours):
    state = load_state()
    if not state["fasting"]:
        state["fasting"] = True
        state["startedAt"] = int(time.time())
        state["targetHours"] = hours
        state["idle"] = False
        save_state(state)
    cmd_status()


def cmd_target(hours):
    state = load_state()
    state["targetHours"] = hours
    save_state(state)
    cmd_status()


def cmd_stop():
    state = load_state()
    if state["fasting"]:
        now = int(time.time())
        actual_hours = round((now - state["startedAt"]) / 3600.0, 2)
        append_history({
            "start": state["startedAt"],
            "end": now,
            "targetHours": state["targetHours"],
            "actualHours": actual_hours,
        })
        state["fasting"] = False
        state["startedAt"] = 0
        # Ending a fast is what opens the eating window, so this is the one
        # transition that deliberately leaves idle off.
        state["idle"] = False
        save_state(state)
    cmd_status()


def cmd_idle():
    """Stop the counter without logging anything.

    Two situations, one outcome: a running fast is discarded (it never reaches
    history, so a mistaken start cannot pad the streak), and an open eating
    window is simply closed. Either way nothing counts afterwards until the
    user starts the next fast.
    """
    state = load_state()
    state["fasting"] = False
    state["startedAt"] = 0
    state["idle"] = True
    save_state(state)
    cmd_status()


def cmd_status():
    state = load_state()
    entries = read_history()
    print(json.dumps({
        "fasting": state["fasting"],
        "startedAt": state["startedAt"],
        "targetHours": state["targetHours"],
        "idle": state["idle"],
        "streak": compute_streak(entries),
        "lastEnd": entries[-1]["end"] if entries else 0,
        "history": recent_long_fasts(entries),
        "longestHistory": longest_fasts(entries),
    }))


def prune_nudges(now):
    for name in os.listdir(NUDGE_DIR):
        path = os.path.join(NUDGE_DIR, name)
        try:
            if now - os.path.getmtime(path) > NUDGE_KEEP_SECONDS:
                os.remove(path)
        except OSError:
            pass


def cmd_nudge(kind, hour, title, body):
    """Send an hourly nudge, but only the first caller to ask for this hour.

    The bar is instantiated once per monitor, so on a multi-monitor desktop
    several copies of the widget reach this point at the same whole hour and
    would otherwise each send the same notification. Creating the marker with
    O_CREAT|O_EXCL is the arbiter: exactly one caller creates it, the rest
    lose the race and return quietly.
    """
    os.makedirs(NUDGE_DIR, exist_ok=True)

    state = load_state()

    # A nudge in flight can outlive the thing it was about: the widget queues
    # it from a 1s tick, and the user may stop the counter in between. Checking
    # here rather than trusting the caller also covers the second bar instance
    # on a multi-monitor desktop, which may not have seen the new state yet.
    if state["idle"] or (kind == "fast" and not state["fasting"]):
        return

    if kind == "fast":
        anchor = state["startedAt"]
    else:
        entries = read_history()
        anchor = entries[-1].get("end", 0) if entries else 0

    marker = os.path.join(NUDGE_DIR, "%s-%d-%d" % (kind, anchor, hour))
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        return

    prune_nudges(time.time())

    try:
        subprocess.run(["notify-send", "-a", "omfasty", title, body], check=False)
    except (OSError, FileNotFoundError):
        pass


def parse_hours(argv, index, default=DEFAULT_TARGET_HOURS):
    if len(argv) > index:
        try:
            return float(argv[index])
        except ValueError:
            pass
    return default


def main():
    argv = sys.argv[1:]
    if not argv:
        cmd_status()
        return
    cmd = argv[0]
    if cmd == "start":
        cmd_start(parse_hours(argv, 1))
    elif cmd == "target":
        cmd_target(parse_hours(argv, 1))
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "idle":
        cmd_idle()
    elif cmd == "status":
        cmd_status()
    elif cmd == "nudge":
        if len(argv) < 5:
            sys.stderr.write("usage: nudge <kind> <hour> <title> <body>\n")
            sys.exit(1)
        try:
            hour = int(argv[2])
        except ValueError:
            sys.exit(1)
        cmd_nudge(argv[1], hour, argv[3], argv[4])
    else:
        sys.stderr.write("unknown command: %s\n" % cmd)
        sys.exit(1)


if __name__ == "__main__":
    main()
