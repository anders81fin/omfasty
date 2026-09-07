# omfasty

An intermittent-fasting timer for the [Omarchy](https://omarchy.org) shell bar. Tracks both sides of the clock — the fast and the eating window that follows it — with a playful, physiology-flavored commentary track instead of a plain countdown.

![omfasty](omfasty-logo.png)

## Features

- **Bar pill** showing elapsed time next to an icon that changes with state: utensils while eating, utensils struck through while fasting, a checkmark once the target is hit.
- **Fasting : eating ratio presets** — 14:10, 16:8, 18:6, 20:4, 22:2, and UMAD? (24:0, the joke option).
- **Eating-window tracking** — once a fast ends, the same pill counts up the time since, against the eating-window target implied by the ratio you picked (`24 - fasting hours`).
- **Progress bar** and a **streak counter** for fasts that hit their target.
- **Physiology-stage commentary** — a tongue-in-cheek line that updates through the fast (blood sugar, glycogen, the metabolic switch, ketosis, autophagy) and through the eating window (fueling up, window closing, into overtime). Not medical advice — it's a bar widget, not a lab.
- **Recent history** — the last five completed fasts with actual vs. target hours.

Click the bar pill to open the popup; pick a ratio to start a fast (or change the target of one already running), and use "End fast" to stop it.

## Installing

```
omarchy plugin add https://github.com/anders81fin/omfasty.git --enable --yes
```

Or by hand: drop this folder into `~/.config/omarchy/plugins/anders81fin.omfasty/`, run `omarchy-shell shell rescanPlugins`, then `omarchy plugin enable anders81fin.omfasty`.

## How it works

State lives outside the shell, in `fasting-cli.py` (a small Python script writing to `~/.local/state/omarchy-fasting/`), so it survives shell restarts and stays simple to inspect or edit by hand. The QML side (`Panel.qml`) just shells out to it and renders the result.

## License

MIT — see [LICENSE](LICENSE).
