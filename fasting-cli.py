#!/usr/bin/env python3
"""State + history backend for the anko11.fasting Omarchy plugin.

Usage:
  fasting-cli.py start <hours>     start a fast (no-op if already fasting)
  fasting-cli.py target <hours>    change the target of an in-progress fast
  fasting-cli.py stop              end the current fast, log it to history
  fasting-cli.py status            print current state + streak + history as JSON
"""
import json
import os
import sys
import time

STATE_DIR = os.path.expanduser("~/.local/state/omarchy-fasting")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
HISTORY_FILE = os.path.join(STATE_DIR, "history.jsonl")
HISTORY_KEEP = 5
DEFAULT_TARGET_HOURS = 16.0


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {
            "fasting": bool(data.get("fasting", False)),
            "startedAt": int(data.get("startedAt", 0)),
            "targetHours": float(data.get("targetHours", DEFAULT_TARGET_HOURS)),
        }
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return {"fasting": False, "startedAt": 0, "targetHours": DEFAULT_TARGET_HOURS}


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


def cmd_start(hours):
    state = load_state()
    if not state["fasting"]:
        state["fasting"] = True
        state["startedAt"] = int(time.time())
        state["targetHours"] = hours
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
        save_state(state)
    cmd_status()


def cmd_status():
    state = load_state()
    entries = read_history()
    print(json.dumps({
        "fasting": state["fasting"],
        "startedAt": state["startedAt"],
        "targetHours": state["targetHours"],
        "streak": compute_streak(entries),
        "lastEnd": entries[-1]["end"] if entries else 0,
        "history": list(reversed(entries[-HISTORY_KEEP:])),
    }))


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
    elif cmd == "status":
        cmd_status()
    else:
        sys.stderr.write("unknown command: %s\n" % cmd)
        sys.exit(1)


if __name__ == "__main__":
    main()
