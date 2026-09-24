#!/usr/bin/env python3
"""Tests for fasting-cli.py. Run with: python3 test-fasting-cli.py

Most cases build a hostile state directory in a throwaway HOME and assert the
CLI neither follows nor destroys anything through it -- the filesystem
hardening asked for in the Omarchy marketplace review. The last group covers
ordinary start/stop/idle behaviour so the hardening cannot quietly break it.

No dependencies and no test runner: this has to be runnable by anyone
reviewing the plugin, on a stock Python.
"""
import json
import os
import subprocess
import sys
import tempfile

CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fasting-cli.py")
failures = []


def run(home, *args, expect_ok=True):
    env = dict(os.environ, HOME=home)
    p = subprocess.run([sys.executable, CLI, *args], env=env,
                       capture_output=True, text=True)
    if expect_ok and p.returncode != 0:
        raise SystemExit("CLI failed on %s: %s" % (args, p.stderr))
    return p


def check(label, got, want):
    ok = got == want
    print("%-52s %s" % (label, "ok" if ok else "FAIL  got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def state_dir(home):
    return os.path.join(home, ".local/state/omarchy-fasting")


# 1. The directory is created private, and a loose one is tightened.
with tempfile.TemporaryDirectory() as home:
    run(home, "start", "16")
    mode = os.stat(state_dir(home)).st_mode & 0o777
    check("state dir is created 0700", oct(mode), oct(0o700))

with tempfile.TemporaryDirectory() as home:
    d = state_dir(home)
    os.makedirs(d, mode=0o755)
    run(home, "start", "16")
    mode = os.stat(d).st_mode & 0o777
    check("a world-readable dir is tightened to 0700", oct(mode), oct(0o700))

# 2. A symlinked state directory is refused outright.
with tempfile.TemporaryDirectory() as home:
    victim = os.path.join(home, "victim")
    os.makedirs(victim)
    os.makedirs(os.path.join(home, ".local/state"))
    os.symlink(victim, state_dir(home))
    p = run(home, "status", expect_ok=False)
    check("a symlinked state dir is refused", p.returncode, 1)
    check("  ...and says why", "cannot open" in p.stderr, True)
    check("  ...and writes nothing through it", os.listdir(victim), [])

# 2b. A symlinked ancestor is refused too: the walk from HOME opens every
#     component with O_NOFOLLOW, not just the last one.
for ancestor in (".local/state", ".local"):
    with tempfile.TemporaryDirectory() as home:
        victim = os.path.join(home, "victim")
        os.makedirs(victim)
        parent = os.path.dirname(os.path.join(home, ancestor))
        os.makedirs(parent, exist_ok=True)
        os.symlink(victim, os.path.join(home, ancestor))
        p = run(home, "start", "16", expect_ok=False)
        check("a symlinked ~/%s is refused" % ancestor, p.returncode, 1)
        check("  ...and nothing is created through it", os.listdir(victim), [])

# 2c. HOME itself is the trusted root and may be a symlink (e.g. /var/home).
with tempfile.TemporaryDirectory() as tmp:
    real = os.path.join(tmp, "real-home")
    os.makedirs(real)
    link = os.path.join(tmp, "home")
    os.symlink(real, link)
    run(link, "start", "16")
    check("a symlinked HOME still works",
          os.path.isfile(os.path.join(state_dir(real), "state.json")), True)

# 3. A symlinked state file is not written through.
with tempfile.TemporaryDirectory() as home:
    d = state_dir(home)
    os.makedirs(d, mode=0o700)
    target = os.path.join(home, "outside.json")
    with open(target, "w") as f:
        f.write("ORIGINAL")
    os.symlink(target, os.path.join(d, "state.json"))

    run(home, "start", "16")
    with open(target) as f:
        check("a symlinked state.json is not followed", f.read(), "ORIGINAL")
    check("  ...the symlink is replaced by a real file",
          os.path.islink(os.path.join(d, "state.json")), False)

# 4. A symlinked history file is not appended through.
with tempfile.TemporaryDirectory() as home:
    d = state_dir(home)
    os.makedirs(d, mode=0o700)
    target = os.path.join(home, "outside.log")
    with open(target, "w") as f:
        f.write("ORIGINAL\n")
    os.symlink(target, os.path.join(d, "history.jsonl"))

    run(home, "start", "16")
    p = run(home, "stop", expect_ok=False)
    with open(target) as f:
        check("a symlinked history is not appended through", f.read(), "ORIGINAL\n")

# 5. The nudge cleanup does not delete through a symlink.
with tempfile.TemporaryDirectory() as home:
    d = state_dir(home)
    nudges = os.path.join(d, "nudges")
    os.makedirs(nudges, mode=0o700)
    victim = os.path.join(home, "precious.txt")
    with open(victim, "w") as f:
        f.write("do not delete me")
    link = os.path.join(nudges, "fast-1-1")
    os.symlink(victim, link)
    old = 1
    os.utime(link, (old, old), follow_symlinks=False)

    run(home, "start", "16")
    run(home, "nudge", "fast", "2", "t", "b")

    check("prune does not delete through a symlink", os.path.exists(victim), True)
    check("  ...and leaves the symlink itself alone", os.path.islink(link), True)

# 6. A hostile `kind` cannot escape the nudge directory.
with tempfile.TemporaryDirectory() as home:
    run(home, "start", "16")
    escape = os.path.join(home, "escape-0-1")
    run(home, "nudge", "../../../../" + home.lstrip("/") + "/escape", "1", "t", "b")
    check("a traversing nudge kind is rejected", os.path.exists(escape), False)

# 7. Bounded history read: a huge file does not blow up, newest survives.
with tempfile.TemporaryDirectory() as home:
    d = state_dir(home)
    os.makedirs(d, mode=0o700)
    with open(os.path.join(d, "history.jsonl"), "w") as f:
        filler = json.dumps({"start": 1, "end": 2, "targetHours": 16.0, "actualHours": 0.1})
        for _ in range(60000):
            f.write(filler + "\n")
        f.write(json.dumps({"start": 10, "end": 1789000000,
                            "targetHours": 16.0, "actualHours": 20.0}) + "\n")
    out = json.loads(run(home, "status").stdout)
    check("an oversized history still parses", out["lastEnd"], 1789000000)

# 8. Ordinary behaviour, so the hardening cannot quietly break it.
def history_lines(home):
    path = os.path.join(state_dir(home), "history.jsonl")
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return len([l for l in f if l.strip()])


with tempfile.TemporaryDirectory() as home:
    out = json.loads(run(home, "status").stdout)
    check("fresh install is idle", (out["fasting"], out["idle"]), (False, True))

    out = json.loads(run(home, "start", "16").stdout)
    check("start begins a fast", (out["fasting"], out["idle"]), (True, False))

    out = json.loads(run(home, "idle").stdout)
    check("idle discards a running fast", (out["fasting"], out["idle"]), (False, True))
    check("a discarded fast is NOT logged", history_lines(home), 0)
    check("a discarded fast leaves no streak", out["streak"], 0)

    run(home, "start", "16")
    out = json.loads(run(home, "stop").stdout)
    check("stop logs and opens the eating window",
          (out["idle"], out["lastEnd"] > 0), (False, True))
    check("stop writes one history entry", history_lines(home), 1)

    out = json.loads(run(home, "idle").stdout)
    check("idle closes the eating window", out["idle"], True)
    check("closing keeps the history", history_lines(home), 1)

    out = json.loads(run(home, "target", "20").stdout)
    check("target is settable while idle", (out["targetHours"], out["idle"]), (20.0, True))

# 9. Upgrade path: a state file written before `idle` existed must keep its
#    eating window running rather than going quiet under the user.
with tempfile.TemporaryDirectory() as home:
    d = state_dir(home)
    os.makedirs(d, mode=0o700)
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump({"fasting": False, "startedAt": 0, "targetHours": 16.0}, f)
    out = json.loads(run(home, "status").stdout)
    check("a legacy state file is not treated as idle", out["idle"], False)

print()
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all checks passed")
