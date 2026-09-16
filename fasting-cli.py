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
import stat
import subprocess
import sys
import time

STATE_DIR = os.path.expanduser("~/.local/state/omarchy-fasting")
STATE_NAME = "state.json"
HISTORY_NAME = "history.jsonl"
NUDGE_NAME = "nudges"
NUDGE_KEEP_SECONDS = 172800
NUDGE_KINDS = ("fast", "eating")
RECENT_COUNT = 3
RECENT_MIN_HOURS = 12.0
LONGEST_COUNT = 3
DEFAULT_TARGET_HOURS = 16.0

# Reads are bounded so a state directory that has grown (or been made to grow)
# cannot pull an unbounded amount into memory. The history cap is generous --
# roughly fifty thousand fasts -- and past it only the newest entries are kept,
# which is the end the streak, the eating-window anchor and the recent list all
# read from anyway.
MAX_STATE_BYTES = 64 * 1024
MAX_HISTORY_BYTES = 4 * 1024 * 1024


# --------------------------------------------------------------------------
# Filesystem access
#
# Everything below goes through a single descriptor for the state directory,
# opened once with O_NOFOLLOW and checked with fstat, and every file is then
# reached *relative to that descriptor* rather than by path. Paths are resolved
# afresh on each syscall and can change underneath the process; a descriptor
# cannot. Without this, a symlink planted in the state directory would be
# followed by the writes here -- and by the nudge cleanup, which deletes.
# --------------------------------------------------------------------------

_state_dir_fd = None


class StateDirError(Exception):
    """The state directory is not something we are willing to write into."""


def state_dir_fd():
    global _state_dir_fd
    if _state_dir_fd is None:
        _state_dir_fd = _open_private_dir(STATE_DIR)
    return _state_dir_fd


def _open_private_dir(path, parent_fd=None):
    """Open a directory we own and only we can read, creating it if needed.

    O_NOFOLLOW refuses a symlink in place of the directory itself; O_DIRECTORY
    refuses a regular file. Ownership is then checked against the real uid, and
    group/other permissions are stripped -- the contents are a health log, and
    early versions created this directory with the process umask.
    """
    try:
        if parent_fd is None:
            os.makedirs(path, mode=0o700, exist_ok=True)
        else:
            os.mkdir(path, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass

    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as e:
        raise StateDirError("cannot open %s safely: %s" % (path, e))

    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise StateDirError("%s is owned by uid %d, not you" % (path, st.st_uid))
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o700)
    except Exception:
        os.close(fd)
        raise

    return fd


def _read_bounded(dir_fd, name, limit, tail=False):
    """Read at most `limit` bytes from a regular file, or b"" if there isn't one.

    A symlink, a directory or a fifo in place of the file reads as empty rather
    than raising, which lands on the same "fall back to defaults" path that a
    corrupt file has always taken.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return b""

    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return b""
        if tail and st.st_size > limit:
            os.lseek(f.fileno(), st.st_size - limit, os.SEEK_SET)
            data = f.read(limit)
            # The seek almost certainly landed mid-line; drop that fragment.
            cut = data.find(b"\n")
            return data[cut + 1:] if cut >= 0 else b""
        return f.read(limit)


def _write_atomic(dir_fd, name, data):
    """Replace a file by writing a fresh temp beside it and renaming over it.

    O_EXCL means an existing temp -- including one someone else planted as a
    symlink -- is an error rather than a target, and the rename is
    descriptor-relative on both ends so neither path is re-resolved.
    """
    tmp = name + ".tmp"
    try:
        os.unlink(tmp, dir_fd=dir_fd)
    except FileNotFoundError:
        pass

    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise

    os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _append_line(dir_fd, name, line):
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
        0o600,
        dir_fd=dir_fd,
    )
    with os.fdopen(fd, "ab") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise StateDirError("%s is not a regular file" % name)
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def load_state():
    try:
        raw = _read_bounded(state_dir_fd(), STATE_NAME, MAX_STATE_BYTES)
        if not raw:
            raise FileNotFoundError
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
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
    _write_atomic(state_dir_fd(), STATE_NAME, json.dumps(state).encode("utf-8"))


def append_history(entry):
    _append_line(state_dir_fd(), HISTORY_NAME, (json.dumps(entry) + "\n").encode("utf-8"))


def read_history():
    raw = _read_bounded(state_dir_fd(), HISTORY_NAME, MAX_HISTORY_BYTES, tail=True)
    entries = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
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


def prune_nudges(nudge_fd, now):
    """Delete stale hour markers.

    This is the one place that removes files, so it is the one that most needs
    to be descriptor-relative: lstat and unlink both run against the directory
    descriptor, and anything that is not a plain file is left alone rather than
    followed. A symlink here would previously have been stat'd and deleted
    through, letting the marker directory act as a lever on any path.
    """
    for name in os.listdir(nudge_fd):
        try:
            st = os.stat(name, dir_fd=nudge_fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                continue
            if now - st.st_mtime > NUDGE_KEEP_SECONDS:
                os.unlink(name, dir_fd=nudge_fd)
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
    # The marker name is built from `kind`, which arrives on the command line.
    # Constraining it to the two kinds this tool knows keeps a separator or a
    # ".." out of the name before it is ever handed to open().
    if kind not in NUDGE_KINDS:
        return

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

    nudge_fd = _open_private_dir(NUDGE_NAME, parent_fd=state_dir_fd())
    try:
        marker = "%s-%d-%d" % (kind, anchor, hour)
        try:
            os.close(os.open(
                marker,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=nudge_fd,
            ))
        except FileExistsError:
            return

        prune_nudges(nudge_fd, time.time())
    finally:
        os.close(nudge_fd)

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
    # A state directory we refuse to touch is a real failure, not a crash to
    # bury: say so on stderr and exit non-zero. The widget reads stdout only,
    # so it simply keeps its last known state rather than rendering a traceback.
    try:
        main()
    except StateDirError as e:
        sys.stderr.write("fasting-cli: %s\n" % e)
        sys.exit(1)
