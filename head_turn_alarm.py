#!/usr/bin/env python3
"""Head Turn Alarm.

Shows you your webcam picture (mirrored, like in a video call) and sounds an
alarm when your head stays turned to the left or right.

Keys (click on the video window first):
  c        calibrate: look straight at the screen and press c
  + / -    alarm angle up / down (less / more sensitive)
  m        sound on / off
  t        keep the window on top of other apps on / off
  1 ... 9  window opacity 0.1 ... 0.9 (0 = 1.0, solid); or click the buttons
  q, Esc   quit
"""

import argparse
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

os.environ.setdefault("GLOG_minloglevel", "2")  # hide mediapipe's info logs

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
CACHE_DIR = Path.home() / ".cache" / "head-turn-alarm"
WINDOW = "Head Turn Alarm"

CALIBRATION_SECONDS = 2.0
HYSTERESIS_DEG = 3.0       # once turned, you must come back up to this far below the angle
LOST_FACE_SECONDS = 15.0   # keep alarming this long if the face vanished while turned
BEEP_EVERY_SECONDS = 1.0
GAUGE_RANGE_DEG = 60.0
OPACITIES = [round(0.1 * i, 1) for i in range(1, 11)]

GREEN, ORANGE, RED = (80, 200, 80), (0, 165, 255), (40, 40, 230)
YELLOW, GRAY, WHITE = (0, 220, 240), (170, 170, 170), (255, 255, 255)
BLACK = (0, 0, 0)
FONT = cv2.FONT_HERSHEY_SIMPLEX


def parse_args():
    p = argparse.ArgumentParser(description="Alarm when your head turns left or right.")
    p.add_argument("--angle", type=float, default=5.0,
                   help="alarm when the head is turned more than this many degrees (default 5)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds the head must stay turned before the alarm (default 1.0)")
    p.add_argument("--camera", type=int, default=0,
                   help="camera number, try 1 or 2 if the wrong camera opens (default 0)")
    p.add_argument("--mute", action="store_true", help="start with the sound off")
    p.add_argument("--no-top", action="store_true",
                   help="don't keep the window on top of other apps")
    p.add_argument("--opacity", type=float, default=0.5,
                   help="window opacity from 0.1 (almost invisible) to 1.0 (solid), default 0.5")
    args = p.parse_args()
    args.opacity = min(1.0, max(0.1, round(args.opacity, 1)))
    return args


def use_system_fonts():
    """OpenCV's Linux window comes without fonts, so its tooltips, status bar and
    Save dialog show no text. Point it to the system fonts (after importing cv2,
    which sets its own empty font folder)."""
    if not sys.platform.startswith("linux"):
        return
    for folder in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu-sans-fonts",
                   "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF",
                   "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/ubuntu"):
        if any(Path(folder).glob("*.ttf")):
            os.environ["QT_QPA_FONTDIR"] = folder
            return


def ensure_model():
    path = CACHE_DIR / "face_landmarker.task"
    if path.exists():
        return path
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading the face model (one time, about 4 MB)...")
    try:
        import ssl
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = None
    part = path.with_suffix(".part")
    with urllib.request.urlopen(MODEL_URL, context=context) as resp, open(part, "wb") as out:
        shutil.copyfileobj(resp, out)
    part.replace(path)
    return path


def write_beep(path):
    """Two-tone beep as a small WAV file, so no sound files need to be shipped."""
    rate = 22050
    samples = bytearray()
    for freq, seconds in ((880, 0.18), (0, 0.06), (660, 0.24)):
        n = int(rate * seconds)
        for i in range(n):
            fade = min(1.0, i / 300, (n - i) / 300)  # soft edges, no clicks
            value = 0.7 * fade * math.sin(2 * math.pi * freq * i / rate) if freq else 0.0
            samples += struct.pack("<h", int(value * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(samples))


class Alarm:
    def __init__(self, enabled):
        self.enabled = enabled
        self.wav = CACHE_DIR / "beep.wav"
        if not self.wav.exists():
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            write_beep(self.wav)
        self.player = self._find_player()
        self.last_ring = 0.0

    @staticmethod
    def _find_player():
        if sys.platform == "darwin":
            return ["afplay"]
        for name, extra in (("paplay", []), ("pw-play", []), ("aplay", ["-q"])):
            if shutil.which(name):
                return [name, *extra]
        return None

    def ring(self, now):
        if not self.enabled or now - self.last_ring < BEEP_EVERY_SECONDS:
            return
        self.last_ring = now
        if self.player:
            subprocess.Popen([*self.player, str(self.wav)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print("\a", end="", flush=True)  # terminal bell as a last resort


def open_camera(index):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        if sys.platform == "darwin":
            hint = ("Allow camera access for your terminal app in "
                    "System Settings > Privacy & Security > Camera, then start again.")
        else:
            hint = ("Check that the camera is connected and not used by another app "
                    "(for example a video call). You can also try --camera 1.")
        sys.exit(f"Could not open camera {index}. {hint}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return cap


def linux_window():
    """(id, x, y, width, height) of our window from wmctrl, or None."""
    if not shutil.which("wmctrl"):
        return None
    try:
        listing = subprocess.run(["wmctrl", "-l", "-G", "-p"], capture_output=True,
                                 text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for row in listing.splitlines():
        parts = row.split(None, 8)  # id, desktop, pid, x, y, width, height, host, title
        if len(parts) == 9 and parts[2] == str(os.getpid()) and parts[8] == WINDOW:
            return (parts[0], *map(int, parts[3:7]))
    return None


def set_on_top(on):
    """Keep the window above other apps and on every workspace. Returns True when
    done, False when the window isn't ready yet, None when this system can't."""
    try:  # macOS and Windows
        cv2.setWindowProperty(WINDOW, cv2.WND_PROP_TOPMOST, 1 if on else 0)
        if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_TOPMOST) == (1 if on else 0):
            return True
    except cv2.error:
        pass
    if not shutil.which("wmctrl"):  # Linux: OpenCV's window can't do it by itself
        return None
    win = linux_window()
    if win is None:
        return False
    action = "add" if on else "remove"
    try:
        subprocess.run(["wmctrl", "-i", "-r", win[0], "-b", f"{action},above,sticky"], timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def mac_set_opacity(alpha):
    """Sets alphaValue of our NSWindow through the Objective-C runtime."""
    import ctypes
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def send(restype, obj, selector, *args):
        prototype = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p,
                                     *(type(a) for a in args))
        return prototype(("objc_msgSend", objc))(obj, objc.sel_registerName(selector), *args)

    app = send(ctypes.c_void_p, objc.objc_getClass(b"NSApplication"), b"sharedApplication")
    windows = send(ctypes.c_void_p, app, b"windows")
    for i in range(send(ctypes.c_ulong, windows, b"count")):
        win = send(ctypes.c_void_p, windows, b"objectAtIndex:", ctypes.c_ulong(i))
        title = send(ctypes.c_void_p, win, b"title")
        if title and send(ctypes.c_char_p, title, b"UTF8String") == WINDOW.encode():
            send(None, win, b"setAlphaValue:", ctypes.c_double(alpha))
            return True
    return False


def set_opacity(alpha):
    """Makes the whole window see-through: 1.0 = solid, 0.1 = almost invisible.
    Returns the same as set_on_top."""
    if sys.platform == "darwin":
        try:
            return mac_set_opacity(alpha)
        except (OSError, AttributeError):
            return None
    if not (shutil.which("wmctrl") and shutil.which("xprop")):
        return None
    win = linux_window()
    if win is None:
        return False
    try:  # the window manager reads this property and fades the whole window
        subprocess.run(["xprop", "-id", win[0], "-f", "_NET_WM_WINDOW_OPACITY", "32c", "-set",
                        "_NET_WM_WINDOW_OPACITY", str(int(alpha * 0xFFFFFFFF))], timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class WindowSetting:
    """Applies a window setting (on top, opacity) once the window exists."""

    def __init__(self, apply, value, initial, hint):
        self.apply, self.value, self.applied, self.hint = apply, value, initial, hint
        self.retry = 0.0

    def update(self, now):
        if self.applied == self.value or now < self.retry:
            return
        done = self.apply(self.value)
        if done is None:  # this system can't do it: say so once and stop trying
            if self.hint:
                print(self.hint)
                self.hint = None
            self.applied = self.value
        elif done:
            self.applied = self.value
        else:
            self.retry = now + 0.5  # window not shown yet, try again soon


def main_screen():
    """(x, y, width, height) of the main screen, or None if it can't be found."""
    try:
        if sys.platform == "darwin":
            import ctypes

            class CGRect(ctypes.Structure):
                _fields_ = [(name, ctypes.c_double) for name in ("x", "y", "w", "h")]

            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
            cg.CGDisplayBounds.restype = CGRect
            r = cg.CGDisplayBounds(cg.CGMainDisplayID())
            return int(r.x), int(r.y), int(r.w), int(r.h)
        out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True,
                             text=True, timeout=2).stdout
        # Lines like " 0: +*HDMI-1 1920/510x1080/290+0+0  HDMI-1"; * marks the main screen.
        monitors = re.findall(r"^\s*\d+: \+?(\*?)\S+ (\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", out, re.M)
        if monitors:
            _, w, h, x, y = max(monitors, key=lambda m: m[0] == "*")
            return int(x), int(y), int(w), int(h)
    except (OSError, AttributeError, subprocess.SubprocessError):
        pass
    return None


class WindowPlacer:
    """Sizes the window to the camera picture and puts it in the middle of the screen.

    The window manager shifts the window by its title bar, and OpenCV's Linux
    toolbar takes room from the picture, so half a second later it measures the
    real window and corrects both once."""

    def __init__(self):
        self.screen = main_screen()
        self.size = None
        self.asked = None   # position passed to moveWindow
        self.fix_at = None

    def centered(self, w, h):
        sx, sy, sw, sh = self.screen
        return sx + (sw - w) // 2, sy + (sh - h) // 2

    def update(self, frame, now):
        if self.size is None:
            fh, fw = frame.shape[:2]
            scale = 1.0
            if self.screen:
                scale = min(1.0, 0.8 * self.screen[2] / fw, 0.8 * self.screen[3] / fh)
            self.size = (int(fw * scale), int(fh * scale))
            cv2.resizeWindow(WINDOW, *self.size)
            if self.screen:
                self.asked = self.centered(*self.size)
                cv2.moveWindow(WINDOW, *self.asked)
            self.fix_at = now + 0.5
        elif self.fix_at is not None and now >= self.fix_at:
            self.fix_at = None
            win = linux_window()
            if win is None or self.asked is None:
                return
            _, x, y, w, h = win
            picture_h = cv2.getWindowImageRect(WINDOW)[3]
            if 0 < picture_h < self.size[1]:  # make room for the toolbar
                h += self.size[1] - picture_h
                cv2.resizeWindow(WINDOW, w, h)
            tx, ty = self.centered(w, h)
            cv2.moveWindow(WINDOW, self.asked[0] + tx - x, self.asked[1] + ty - y)


def head_yaw(result):
    """Head turn in degrees: positive = turned to your left, negative = to your right."""
    if not result.facial_transformation_matrixes:
        return None
    m = result.facial_transformation_matrixes[0]
    return math.degrees(math.atan2(-m[2][0], math.hypot(m[2][1], m[2][2])))


def put_text(img, text, org, scale, color, thickness=2):
    """Text on a darkened band, so it stays readable on any background."""
    (tw, th), base = cv2.getTextSize(text, FONT, scale, thickness)
    x, y = org
    pad = max(4, th // 3)
    y1, y2 = max(0, y - th - pad), min(img.shape[0], y + base + pad)
    x1, x2 = max(0, x - pad), min(img.shape[1], x + tw + pad)
    img[y1:y2, x1:x2] = (img[y1:y2, x1:x2] * 0.35).astype(img.dtype)
    cv2.putText(img, text, org, FONT, scale, color, thickness, cv2.LINE_AA)


def opacity_buttons(w):
    """Boxes of the opacity buttons at the top right: [(value, x1, y1, x2, y2), ...]."""
    s = w / 640
    bw, bh, gap = int(40 * s), int(24 * s), int(5 * s)
    x0 = w - int(24 * s) - len(OPACITIES) * (bw + gap) + gap
    y1 = int(68 * s)
    return [(v, x0 + i * (bw + gap), y1, x0 + i * (bw + gap) + bw, y1 + bh)
            for i, v in enumerate(OPACITIES)]


def draw_opacity_buttons(view, opacity):
    s = view.shape[1] / 640
    thickness = max(1, int(s))
    buttons = opacity_buttons(view.shape[1])
    _, bx, by1, _, by2 = buttons[0]
    (tw, th), _ = cv2.getTextSize("Opacity", FONT, 0.5 * s, thickness)
    put_text(view, "Opacity", (bx - tw - int(12 * s), (by1 + by2 + th) // 2), 0.5 * s, WHITE, thickness)
    for value, x1, y1, x2, y2 in buttons:
        selected = abs(value - opacity) < 0.05
        if selected:
            cv2.rectangle(view, (x1, y1), (x2, y2), WHITE, -1)
        else:
            view[y1:y2, x1:x2] = (view[y1:y2, x1:x2] * 0.35).astype(view.dtype)
            cv2.rectangle(view, (x1, y1), (x2, y2), GRAY, 1)
        label = f"{value:.1f}"
        (tw, th), _ = cv2.getTextSize(label, FONT, 0.45 * s, thickness)
        cv2.putText(view, label, ((x1 + x2 - tw) // 2, (y1 + y2 + th) // 2), FONT, 0.45 * s,
                    BLACK if selected else WHITE, thickness, cv2.LINE_AA)


def draw(view, status, color, rel, angle, alarming, sound_on, on_top, opacity):
    h, w = view.shape[:2]
    s = w / 640

    if alarming:
        cv2.rectangle(view, (0, 0), (w - 1, h - 1), RED, int(18 * s))
    put_text(view, status, (int(20 * s), int(45 * s)), 1.0 * s, color, max(2, int(2 * s)))

    # Gauge: the view is mirrored, so turning to your left moves the dot to the left.
    cx, cy, half = w // 2, h - int(60 * s), int(w * 0.35)
    cv2.line(view, (cx - half, cy), (cx + half, cy), GRAY, max(2, int(3 * s)))
    for sign in (-1, 1):
        x = cx + int(sign * half * angle / GAUGE_RANGE_DEG)
        cv2.line(view, (x, cy - int(12 * s)), (x, cy + int(12 * s)), ORANGE, max(2, int(3 * s)))
    cv2.line(view, (cx, cy - int(8 * s)), (cx, cy + int(8 * s)), WHITE, 1)
    if rel is not None:
        clipped = max(-GAUGE_RANGE_DEG, min(GAUGE_RANGE_DEG, rel))
        x = cx - int(half * clipped / GAUGE_RANGE_DEG)
        cv2.circle(view, (x, cy), int(10 * s), color, -1, cv2.LINE_AA)

    help_text = (f"c: calibrate  +/-: angle {angle:.0f}  m: sound {'on' if sound_on else 'OFF'}  "
                 f"t: on top{'' if on_top else ' OFF'}  q: quit")
    put_text(view, help_text, (int(20 * s), h - int(18 * s)), 0.5 * s, WHITE, 1)
    draw_opacity_buttons(view, opacity)


def main():
    args = parse_args()
    landmarker = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(ensure_model())),
        running_mode=vision.RunningMode.VIDEO,
        output_facial_transformation_matrixes=True,
    ))
    alarm = Alarm(enabled=not args.mute)
    cap = open_camera(args.camera)
    use_system_fonts()
    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    placer = WindowPlacer()
    on_top = WindowSetting(set_on_top, not args.no_top, False,
                           "Can't keep the window on top here. On Linux install wmctrl "
                           "(sudo apt install wmctrl), or right-click the window title "
                           "and choose 'Always on Top'.")
    opacity = WindowSetting(set_opacity, args.opacity, 1.0,
                            "Can't make the window see-through here. On Linux install "
                            "wmctrl and xprop (sudo apt install wmctrl x11-utils).")
    clicks = []
    cv2.setMouseCallback(WINDOW, lambda event, x, y, flags, param:
                         clicks.append((x, y)) if event == cv2.EVENT_LBUTTONDOWN else None)

    angle = args.angle
    baseline = None             # head yaw when looking straight at the screen
    calibration = []            # (time, yaw) samples while calibrating
    smooth = None               # smoothed yaw, removes jitter
    last_rel, last_seen = None, 0.0
    turned_since = None
    calibrated_at = None
    last_ts = 0
    bad_frames = 0

    print("Look straight at the screen for 2 seconds to calibrate. Press q in the window to quit.")
    while True:
        ok, frame = cap.read()
        if not ok:
            bad_frames += 1
            if bad_frames > 50:
                sys.exit("The camera stopped sending pictures.")
            time.sleep(0.05)
            continue
        bad_frames = 0

        now = time.monotonic()
        last_ts = max(last_ts + 1, int(now * 1000))  # mediapipe needs rising timestamps
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        yaw = head_yaw(landmarker.detect_for_video(
            mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), last_ts))

        rel = None
        if yaw is not None:
            smooth = yaw if smooth is None else smooth + 0.4 * (yaw - smooth)
            if baseline is None:
                calibration.append((now, smooth))
                if now - calibration[0][0] >= CALIBRATION_SECONDS:
                    values = sorted(v for _, v in calibration)
                    baseline = values[len(values) // 2]
                    calibrated_at = now
                    print(f"Calibrated (camera sees your straight head at {baseline:+.0f} deg).")
            if baseline is not None:
                rel = smooth - baseline
                last_rel, last_seen = rel, now
        else:
            smooth = None
            calibration.clear()
            # Turned so far that the face can't be tracked: treat as still turned.
            if (baseline is not None and last_rel is not None and abs(last_rel) >= angle
                    and now - last_seen < LOST_FACE_SECONDS):
                rel = last_rel

        hysteresis = min(HYSTERESIS_DEG, angle * 0.4)
        limit = angle - hysteresis if turned_since is not None else angle
        if rel is not None and abs(rel) >= limit:
            if turned_since is None:
                turned_since = now
        else:
            turned_since = None
        alarming = turned_since is not None and now - turned_since >= args.delay
        if alarming:
            alarm.ring(now)

        if baseline is None:
            status, color = ("Look straight at the screen - calibrating..." if yaw is not None
                             else "No face found - calibrating..."), YELLOW
        elif now - calibrated_at < 2.0:
            status, color = "Saved as your straight position", GREEN
        elif rel is None:
            status, color = "No face found", GRAY
        else:
            side = "LEFT" if rel > 0 else "RIGHT"
            if alarming:
                status, color = f"HEAD TURNED {side}  {abs(rel):.0f} deg", RED
            elif turned_since is not None:
                status, color = f"Turning {side.lower()}  {abs(rel):.0f} deg", ORANGE
            else:
                status, color = f"Straight  {abs(rel):.0f} deg", GREEN

        view = cv2.flip(frame, 1)  # mirror, like a video call self-view
        draw(view, status, color, rel, angle, alarming, alarm.enabled, on_top.value, opacity.value)
        cv2.imshow(WINDOW, view)
        placer.update(view, now)
        on_top.update(now)
        opacity.update(now)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27) or cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
            break
        for x, y in clicks:
            for value, x1, y1, x2, y2 in opacity_buttons(view.shape[1]):
                if x1 <= x <= x2 and y1 <= y <= y2:
                    opacity.value = value
        clicks.clear()
        if key == ord("c"):
            baseline, last_rel, turned_since = None, None, None
            calibration.clear()
        elif key in (ord("+"), ord("=")):
            angle = min(60.0, angle + 1)
        elif key in (ord("-"), ord("_")):
            angle = max(2.0, angle - 1)
        elif key == ord("m"):
            alarm.enabled = not alarm.enabled
        elif key == ord("t"):
            on_top.value = not on_top.value
        elif ord("1") <= key <= ord("9"):
            opacity.value = (key - ord("0")) / 10
        elif key == ord("0"):
            opacity.value = 1.0

    cap.release()
    cv2.destroyAllWindows()
    landmarker.close()


if __name__ == "__main__":
    main()
