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
import math
import os
import pwd
import stat
import subprocess
import sys
import time

# The state directory, as components below $HOME. It is reached one component
# at a time from a descriptor for $HOME, never as a single path -- see below.
STATE_DIR_PARTS = (".local", "state", "omarchy-fasting")
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

# Numbers from the command line and from disk are held to these before they are
# stored or printed. NaN and infinity in particular must never reach the JSON:
# Python writes them as bare NaN/Infinity, which QML's JSON.parse rejects, and
# the widget would then stop reading state altogether.
MAX_TARGET_HOURS = 24.0 * 14
MAX_TIMESTAMP = 2 ** 53
MAX_NUDGE_HOUR = 24 * 365

# notify-send by absolute path, so the nudge does not run whatever a PATH
# entry happens to provide under that name. First one that exists wins.
NOTIFY_SEND_PATHS = ("/usr/bin/notify-send", "/bin/notify-send")


# --------------------------------------------------------------------------
# Filesystem access
#
# Everything below goes through a single descriptor for the state directory,
# reached by walking down from $HOME one component at a time -- each opened
# relative to the previous one with O_NOFOLLOW and checked with fstat, so no
# ancestor can be a symlink or be swapped out mid-walk -- and every file is then
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
        _state_dir_fd = _open_state_dir()
    return _state_dir_fd


def _open_state_dir():
    """Walk from $HOME to the state directory, one descriptor at a time.

    The home directory is the trusted root. It comes from the password database
    rather than $HOME, which is only an environment variable; it is opened by
    path (it may legitimately be a symlink, e.g. into /var/home) but must be
    owned by us. Every component
    below it is created and opened relative to its parent's descriptor, so a
    symlink or a swapped directory anywhere along ~/.local/state is refused
    rather than followed. Only the final directory is made private; the shared
    XDG ancestors keep whatever permissions they already have.
    """
    home = _home_dir()
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise StateDirError("cannot open %s safely: %s" % (home, e))
    try:
        _check_owner(fd, home)
    except Exception:
        os.close(fd)
        raise

    path = home
    last = len(STATE_DIR_PARTS) - 1
    for i, name in enumerate(STATE_DIR_PARTS):
        path = os.path.join(path, name)
        try:
            child = _open_owned_dir(name, fd, path, private=(i == last))
        finally:
            os.close(fd)
        fd = child
    return fd


def _home_dir():
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        home = ""
    if not home:
        raise StateDirError("no home directory for uid %d" % os.getuid())
    return home


def _open_owned_dir(name, parent_fd, path=None, private=True):
    """Open directory `name` under `parent_fd`, creating it if needed.

    O_NOFOLLOW refuses a symlink in place of the directory; O_DIRECTORY refuses
    a regular file. Ownership is then checked against the real uid. With
    `private`, group/other permissions are stripped -- the contents are a health
    log, and early versions created the state directory with the process umask.
    `path` is only used in error messages.
    """
    path = path or name
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as e:
        raise StateDirError("cannot create %s: %s" % (path, e))

    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as e:
        raise StateDirError("cannot open %s safely: %s" % (path, e))

    try:
        st = _check_owner(fd, path)
        if private and st.st_mode & 0o077:
            os.fchmod(fd, 0o700)
    except Exception:
        os.close(fd)
        raise

    return fd


def _check_owner(fd, path):
    st = os.fstat(fd)
    if st.st_uid != os.getuid():
        raise StateDirError("%s is owned by uid %d, not you" % (path, st.st_uid))
    return st


def _open_regular(dir_fd, name, flags, mode=0o600):
    """Open `name` under `dir_fd` only if it is a plain, singly-linked file.

    O_NOFOLLOW refuses a symlink. O_NONBLOCK keeps a fifo planted in place of
    the file from blocking the open forever -- the S_ISREG check below would
    otherwise never be reached -- and is cleared again once the file is known to
    be regular. A second hard link means the same inode is reachable from
    outside the state directory, which O_NOFOLLOW cannot see, so that is
    refused too. Raises StateDirError.
    """
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=dir_fd)
    except OSError as e:
        raise StateDirError("cannot open %s safely: %s" % (name, e))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StateDirError("%s is not a regular file" % name)
        if st.st_nlink != 1:
            raise StateDirError("%s has %d hard links" % (name, st.st_nlink))
        os.set_blocking(fd, True)
    except Exception:
        os.close(fd)
        raise
    return fd, st


def _read_bounded(dir_fd, name, limit, tail=False):
    """Read at most `limit` bytes from a regular file, or b"" if there isn't one.

    Anything _open_regular refuses reads as empty rather than raising, which
    lands on the same "fall back to defaults" path that a corrupt file has
    always taken.
    """
    try:
        fd, st = _open_regular(dir_fd, name, os.O_RDONLY)
    except StateDirError:
        return b""

    with os.fdopen(fd, "rb") as f:
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
    descriptor-relative on both ends so neither path is re-resolved. The temp
    name is unique per call: with one bar per monitor, two writers can run at
    once, and a shared name had each deleting the other's temp mid-write. The
    directory is fsynced after the rename so the new entry itself survives a
    power cut, not just the file's contents.
    """
    tmp = "%s.%d.%s.tmp" % (name, os.getpid(), os.urandom(4).hex())
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

    try:
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except Exception:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    os.fsync(dir_fd)


def _append_line(dir_fd, name, line):
    fd, _ = _open_regular(dir_fd, name, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, "ab") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _number(value, low, high):
    """`value` as a float if it is a finite JSON number in [low, high], else None.

    bool is excluded explicitly: it is an int subclass, and `true` is not an
    hour count.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def _target_hours(value):
    hours = _number(value, 0.0, MAX_TARGET_HOURS)
    return hours if hours and hours > 0 else DEFAULT_TARGET_HOURS


def load_state():
    try:
        raw = _read_bounded(state_dir_fd(), STATE_NAME, MAX_STATE_BYTES)
        if not raw:
            raise FileNotFoundError
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        started = _number(data.get("startedAt", 0), 0, MAX_TIMESTAMP)
        return {
            "fasting": bool(data.get("fasting", False)),
            "startedAt": int(started) if started is not None else 0,
            "targetHours": _target_hours(data.get("targetHours", DEFAULT_TARGET_HOURS)),
            # Nothing is counting: neither a fast nor the eating window. Absent
            # from state files written before this flag existed, and defaulting
            # to False there is what keeps their eating window running as it
            # did rather than silently stopping on upgrade.
            "idle": bool(data.get("idle", False)),
        }
    except (FileNotFoundError, ValueError, RecursionError):
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


def _history_entry(entry):
    """The four fields stop() writes, as finite numbers, or None to skip the line.

    Only these fields are passed on: the widget formats them with toFixed()
    and Date(), and anything else in the file has no business reaching it.
    """
    if not isinstance(entry, dict):
        return None
    start = _number(entry.get("start"), 0, MAX_TIMESTAMP)
    end = _number(entry.get("end"), 0, MAX_TIMESTAMP)
    target = _number(entry.get("targetHours"), 0.0, MAX_TARGET_HOURS)
    actual = _number(entry.get("actualHours"), 0.0, MAX_TIMESTAMP / 3600.0)
    if None in (start, end, target, actual):
        return None
    return {"start": int(start), "end": int(end), "targetHours": target, "actualHours": actual}


def read_history():
    raw = _read_bounded(state_dir_fd(), HISTORY_NAME, MAX_HISTORY_BYTES, tail=True)
    entries = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = _history_entry(json.loads(line))
        except (ValueError, RecursionError):
            continue
        if entry is not None:
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
    # The hour goes into the same name; bounded, it cannot outgrow NAME_MAX.
    if not 0 <= hour <= MAX_NUDGE_HOUR:
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

    nudge_fd = _open_owned_dir(NUDGE_NAME, state_dir_fd())
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

    notify_send = next((p for p in NOTIFY_SEND_PATHS if os.access(p, os.X_OK)), None)
    if notify_send is None:
        return
    try:
        # "--" so a title or body starting with "-" is text, not an option.
        subprocess.run([notify_send, "-a", "omfasty", "--", title, body], check=False)
    except OSError:
        pass


def parse_hours(argv, index):
    """The hour count at argv[index]; the default if missing or not a sane number.

    float() alone accepts "nan" and "inf", which is how a NaN target reached
    the state file.
    """
    if len(argv) > index:
        try:
            return _target_hours(float(argv[index]))
        except ValueError:
            pass
    return DEFAULT_TARGET_HOURS


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


def run():
    # A state directory we refuse to touch is a real failure, not a crash to
    # bury: say so on stderr and exit non-zero. The widget reads stdout only,
    # so it simply keeps its last known state rather than rendering a traceback.
    try:
        main()
    except StateDirError as e:
        sys.stderr.write("fasting-cli: %s\n" % e)
        sys.exit(1)


if __name__ == "__main__":
    run()
