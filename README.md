# Head Turn Alarm

Shows your webcam picture (mirrored, like in a video call) and beeps when your
head stays turned to the left or right.

## Start

macOS or Linux, in a terminal in this folder:

```bash
./run.sh
```

The first start installs what it needs (a minute or two, needs internet).
On macOS, allow camera access for your terminal app when asked.

## Use

1. Look straight at the screen for 2 seconds. The program remembers this as "straight"
   and shows "Saved as your straight position".
2. If you turn your head more than 5 degrees for more than 1 second, the window
   gets a red frame and it beeps until you turn back.
3. The window opens in the middle of the screen and stays on top of other apps
   (on Linux also on every workspace), so you can see it while you work.
4. The window is half see-through (opacity 0.5). Click the buttons 0.1 ... 1.0 at the
   top of the window to change it (1.0 = solid).

How it knows "straight": it doesn't know where your camera is. It finds your face,
measures which way your head points (it follows your nose and face shape, not your
eyes), and saves the direction you were facing during those first 2 seconds.
So if you weren't looking straight at the screen then, look straight and press `c`.

Keys (click the video window first):

| Key     | What it does                                  |
|---------|-----------------------------------------------|
| `c`     | calibrate again (look straight, then press c) |
| `+` `-` | alarm angle up / down                         |
| `m`     | sound on / off                                |
| `t`     | keep the window on top of other apps on / off |
| `1`-`9` | opacity 0.1 - 0.9 (`0` = 1.0, solid)          |
| `q`     | quit                                          |

Options:

```bash
./run.sh --angle 10 --delay 0.5
```

- `--angle 10` alarm only at a bigger turn (default is 5)
- `--delay 0.5` alarm sooner
- `--camera 1` use another camera
- `--mute` start without sound
- `--no-top` don't keep the window on top of other apps
- `--opacity 0.8` start with another opacity (0.1 - 1.0, default 0.5)

## Notes

- Linux: usually only one app can use the camera at a time, so close video calls first.
  On macOS the camera can be shared.
- Linux: keeping the window on top, centering it and opacity use `wmctrl` and `xprop`
  (`sudo apt install wmctrl x11-utils`).
  Without it, right-click the window title and choose "Always on Top".
- macOS: works the same way, except the window shows only on the current desktop (Space)
  and has no toolbar with the Save button.
- Intel Macs need Python 3.12 or older (newer Python has no face library for Intel Macs yet).
