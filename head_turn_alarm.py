#!/usr/bin/env python3
"""Head Turn Alarm.

Shows you your webcam picture (mirrored, like in a video call) and sounds an
alarm when your head stays turned to the left or right.

All controls are in the toolbar. Keys (click on the window first):
  c        calibrate: look straight at the screen and press c
  + / -    alarm angle up / down (less / more sensitive)
  m        sound on / off
  t        keep the window on top of other apps on / off
  1 ... 9  picture opacity 0.1 ... 0.9 (0 = 1.0, solid)
  q, Esc   quit
"""

import argparse
import math
import os
import shutil
import signal
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

# OpenCV points Qt to its own Qt 5 plugins and fonts; this window uses Qt 6.
for name in ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR"):
    os.environ.pop(name, None)
if sys.platform.startswith("linux"):
    # On Wayland apps can't stay on top or place their window; through X11 (XWayland) they can.
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QLabel, QMainWindow,
                               QSpinBox, QToolBar, QWidget)

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
MAX_ZOOM = 3.0

GREEN, ORANGE, RED = "#50c850", "#ffa500", "#e62828"
YELLOW, GRAY, WHITE = "#f0dc00", "#aaaaaa", "#ffffff"


def parse_args():
    p = argparse.ArgumentParser(description="Alarm when your head turns left or right.")
    p.add_argument("--angle", type=int, default=5,
                   help="alarm when the head is turned more than this many degrees (default 5)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds the head must stay turned before the alarm (default 1.0)")
    p.add_argument("--camera", type=int, default=0,
                   help="camera number, try 1 or 2 if the wrong camera opens (default 0)")
    p.add_argument("--mute", action="store_true", help="start with the sound off")
    p.add_argument("--no-top", action="store_true",
                   help="don't keep the window on top of other apps")
    p.add_argument("--opacity", type=float, default=0.5,
                   help="camera picture opacity from 0.1 (almost see-through) to 1.0 (solid), "
                        "default 0.5")
    args = p.parse_args()
    args.angle = min(60, max(2, args.angle))
    args.opacity = min(1.0, max(0.1, round(args.opacity, 1)))
    return args


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


def wmctrl_set(states, on):
    """Linux: add or remove window states ("above", "sticky" = all workspaces) of this
    app's window. Returns False when wmctrl is missing or the window isn't shown yet."""
    if not (sys.platform.startswith("linux") and shutil.which("wmctrl")):
        return False
    try:
        listing = subprocess.run(["wmctrl", "-l", "-p"], capture_output=True,
                                 text=True, timeout=2).stdout
        ids = [parts[0] for parts in (row.split(None, 4) for row in listing.splitlines())
               if len(parts) == 5 and parts[2] == str(os.getpid()) and parts[4] == WINDOW]
        for window_id in ids:
            subprocess.run(["wmctrl", "-i", "-r", window_id, "-b",
                            f"{'add' if on else 'remove'},{states}"], timeout=2)
        return bool(ids)
    except (OSError, subprocess.SubprocessError):
        return False


class FaceTracker:
    def __init__(self):
        self.landmarker = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(ensure_model())),
            running_mode=vision.RunningMode.VIDEO,
            output_facial_transformation_matrixes=True,
        ))
        self.last_ts = 0

    def head_yaw(self, frame, now):
        """Head turn in degrees: positive = turned to your left, negative = to your
        right, None = no face found."""
        self.last_ts = max(self.last_ts + 1, int(now * 1000))  # mediapipe needs rising timestamps
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = self.landmarker.detect_for_video(
            mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), self.last_ts)
        if not result.facial_transformation_matrixes:
            return None
        m = result.facial_transformation_matrixes[0]
        return math.degrees(math.atan2(-m[2][0], math.hypot(m[2][1], m[2][2])))

    def close(self):
        self.landmarker.close()


class HeadMonitor:
    """Decides straight / turning / turned, relative to the calibrated straight position."""

    def __init__(self, angle, delay):
        self.angle, self.delay = angle, delay
        self.calibrate()

    def calibrate(self):
        self.baseline = None        # head yaw when looking straight at the screen
        self.samples = []           # (time, yaw) samples while calibrating
        self.calibrated_at = None
        self.smooth = None          # smoothed yaw, removes jitter
        self.last_rel, self.last_seen = None, 0.0
        self.turned_since = None

    def update(self, yaw, now):
        """Returns (status text, color, degrees from straight or None, alarming)."""
        rel = None
        if yaw is not None:
            self.smooth = yaw if self.smooth is None else self.smooth + 0.4 * (yaw - self.smooth)
            if self.baseline is None:
                self.samples.append((now, self.smooth))
                if now - self.samples[0][0] >= CALIBRATION_SECONDS:
                    values = sorted(v for _, v in self.samples)
                    self.baseline = values[len(values) // 2]
                    self.calibrated_at = now
                    print(f"Calibrated (camera sees your straight head at {self.baseline:+.0f} deg).")
            if self.baseline is not None:
                rel = self.smooth - self.baseline
                self.last_rel, self.last_seen = rel, now
        else:
            self.smooth = None
            self.samples.clear()
            # Turned so far that the face can't be tracked: treat as still turned.
            if (self.baseline is not None and self.last_rel is not None
                    and abs(self.last_rel) >= self.angle
                    and now - self.last_seen < LOST_FACE_SECONDS):
                rel = self.last_rel

        hysteresis = min(HYSTERESIS_DEG, self.angle * 0.4)
        limit = self.angle - hysteresis if self.turned_since is not None else self.angle
        if rel is not None and abs(rel) >= limit:
            if self.turned_since is None:
                self.turned_since = now
        else:
            self.turned_since = None
        alarming = self.turned_since is not None and now - self.turned_since >= self.delay

        if self.baseline is None:
            status = ("Look straight at the screen - calibrating..." if yaw is not None
                      else "No face found - calibrating...")
            return status, YELLOW, None, False
        if now - self.calibrated_at < 2.0:
            return "Saved as your straight position", GREEN, rel, alarming
        if rel is None:
            return "No face found", GRAY, None, False
        side = "LEFT" if rel > 0 else "RIGHT"
        if alarming:
            return f"HEAD TURNED {side}  {abs(rel):.0f}°", RED, rel, True
        if self.turned_since is not None:
            return f"Turning {side.lower()}  {abs(rel):.0f}°", ORANGE, rel, False
        return f"Straight  {abs(rel):.0f}°", GREEN, rel, False


class VideoView(QWidget):
    """The camera picture, see-through by `opacity`, with a solid status line,
    gauge and red alarm frame on top."""

    def __init__(self, opacity, angle):
        super().__init__()
        self.opacity, self.zoom, self.angle = opacity, 1.0, angle
        self.image = None
        self.status, self.color, self.rel, self.alarming = "Starting the camera...", GRAY, None, False
        self.setMinimumSize(320, 240)

    def show_frame(self, frame, status, color, rel, alarming, angle):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        self.image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        self.status, self.color, self.rel, self.alarming, self.angle = status, color, rel, alarming, angle
        self.update()

    def source_rect(self):
        """Part of the picture to show: fills the view without stretching, cut by the zoom."""
        iw, ih = self.image.width(), self.image.height()
        scale = max(self.width() / iw, self.height() / ih) * self.zoom
        sw, sh = self.width() / scale, self.height() / scale
        return QRectF((iw - sw) / 2, (ih - sh) / 2, sw, sh)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        view = QRectF(self.rect())
        if self.image is not None:
            p.setOpacity(self.opacity)
            p.drawImage(view, self.image, self.source_rect())
            p.setOpacity(1.0)
        s = view.width() / 640

        if self.alarming:
            p.setPen(QPen(QColor(RED), 14 * s))
            p.drawRect(view.adjusted(7 * s, 7 * s, -7 * s, -7 * s))

        font = p.font()
        font.setPixelSize(max(12, int(24 * s)))
        font.setBold(True)
        p.setFont(font)
        metrics = p.fontMetrics()
        band = QRectF(14 * s, 14 * s, metrics.horizontalAdvance(self.status) + 24 * s,
                      metrics.height() + 12 * s)
        p.fillRect(band, QColor(0, 0, 0, 170))
        p.setPen(QColor(self.color))
        p.drawText(band, Qt.AlignmentFlag.AlignCenter, self.status)

        # Gauge: the picture is mirrored, so turning to your left moves the dot to the left.
        cx, cy, half = view.width() / 2, view.height() - 34 * s, view.width() * 0.35
        p.setPen(QPen(QColor(GRAY), 3 * s))
        p.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))
        p.setPen(QPen(QColor(ORANGE), 3 * s))
        for sign in (-1, 1):
            x = cx + sign * half * self.angle / GAUGE_RANGE_DEG
            p.drawLine(QPointF(x, cy - 12 * s), QPointF(x, cy + 12 * s))
        p.setPen(QPen(QColor(WHITE), 1))
        p.drawLine(QPointF(cx, cy - 8 * s), QPointF(cx, cy + 8 * s))
        if self.rel is not None:
            clipped = max(-GAUGE_RANGE_DEG, min(GAUGE_RANGE_DEG, self.rel))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(self.color))
            p.drawEllipse(QPointF(cx - half * clipped / GAUGE_RANGE_DEG, cy), 10 * s, 10 * s)


class MainWindow(QMainWindow):
    def __init__(self, args, cap, tracker, alarm):
        super().__init__()
        self.cap, self.tracker, self.alarm = cap, tracker, alarm
        self.monitor = HeadMonitor(args.angle, args.delay)
        self.frame = None           # last mirrored camera picture, for Save image
        self.bad_frames = 0

        self.setWindowTitle(WINDOW)
        # Lets the picture be see-through while the toolbar stays solid.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.view = VideoView(args.opacity, args.angle)
        self.setCentralWidget(self.view)
        self.build_toolbar(args)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, not args.no_top)
        self.resize(640, 480 + self.toolbar.sizeHint().height())

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(0)

    def add_button(self, text, tip, slot, checked=None, shortcut=None):
        action = QAction(text, self)
        action.setToolTip(tip)
        if shortcut:
            action.setShortcut(shortcut)
        if checked is None:
            action.triggered.connect(slot)
        else:
            action.setCheckable(True)
            action.setChecked(checked)
            action.toggled.connect(slot)
        self.toolbar.addAction(action)
        return action

    def build_toolbar(self, args):
        bar = self.toolbar = QToolBar()
        bar.setMovable(False)
        bar.setAutoFillBackground(True)  # solid, even when the picture is see-through
        self.addToolBar(bar)

        self.add_button("Calibrate", "Look straight at the screen, then click (key c)",
                        self.monitor.calibrate)
        bar.addSeparator()
        bar.addWidget(QLabel(" Angle "))
        self.angle_box = QSpinBox()
        self.angle_box.setRange(2, 60)
        self.angle_box.setSuffix("°")
        self.angle_box.setValue(args.angle)
        self.angle_box.setToolTip("Alarm when the head turns more than this (keys + and -)")
        self.angle_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep keys for the window
        self.angle_box.valueChanged.connect(lambda value: setattr(self.monitor, "angle", value))
        bar.addWidget(self.angle_box)
        bar.addSeparator()
        self.sound_action = self.add_button("Sound", "Beep on / off (key m)",
                                            lambda on: setattr(self.alarm, "enabled", on),
                                            checked=not args.mute)
        self.top_action = self.add_button("On top", "Keep this window over other apps (key t)",
                                          self.set_on_top, checked=not args.no_top)
        bar.addSeparator()
        bar.addWidget(QLabel(" Opacity "))
        self.opacity_box = QComboBox()
        self.opacity_box.addItems([f"{value:.1f}" for value in OPACITIES])
        self.opacity_box.setCurrentIndex(OPACITIES.index(args.opacity))
        self.opacity_box.setToolTip("How see-through the camera picture is, 1.0 = solid (keys 1-9, 0)")
        self.opacity_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.opacity_box.currentIndexChanged.connect(self.set_opacity)
        bar.addWidget(self.opacity_box)
        bar.addSeparator()
        self.add_button("Zoom in", "Zoom in (Ctrl +)", lambda: self.set_zoom(1.25),
                        shortcut=QKeySequence.StandardKey.ZoomIn)
        self.add_button("Zoom out", "Zoom out (Ctrl -)", lambda: self.set_zoom(1 / 1.25),
                        shortcut=QKeySequence.StandardKey.ZoomOut)
        self.add_button("Save image", "Save the current camera picture (Ctrl S)", self.save_image,
                        shortcut=QKeySequence.StandardKey.Save)

    def set_opacity(self, index):
        self.view.opacity = OPACITIES[index]
        self.view.update()

    def set_zoom(self, factor):
        self.view.zoom = min(MAX_ZOOM, max(1.0, self.view.zoom * factor))
        self.view.update()

    def set_on_top(self, on):
        # On Linux wmctrl changes it in place (and all workspaces too); Qt would
        # have to re-create the window.
        if not wmctrl_set("above,sticky", on):
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
            self.show()  # changing window flags hides the window

    def show_on_all_workspaces(self, tries=10):
        """Linux: keep the window on every workspace, once it is on screen."""
        if (not wmctrl_set("sticky", True) and tries > 1
                and sys.platform.startswith("linux") and shutil.which("wmctrl")):
            QTimer.singleShot(300, lambda: self.show_on_all_workspaces(tries - 1))

    def center_on_screen(self):
        frame = self.frameGeometry()
        frame.moveCenter(self.screen().availableGeometry().center())
        self.move(frame.topLeft())

    def save_image(self):
        if self.frame is None:
            return
        frame = self.frame.copy()  # the picture at the moment of the click
        pictures = Path.home() / "Pictures"
        folder = pictures if pictures.is_dir() else Path.home()
        default = folder / time.strftime("head-turn-%Y%m%d-%H%M%S.png")
        path, _ = QFileDialog.getSaveFileName(self, "Save image", str(default),
                                              "Images (*.png *.jpg)")
        if not path:
            return
        if Path(path).suffix.lower() not in (".png", ".jpg", ".jpeg"):
            path += ".png"
        if not cv2.imwrite(path, frame):
            print(f"Could not save {path}")

    def keyPressEvent(self, event):
        key = event.text().lower()
        if key == "q" or event.key() == Qt.Key.Key_Escape:
            self.close()
        elif key == "c":
            self.monitor.calibrate()
        elif key in ("+", "="):
            self.angle_box.stepUp()
        elif key in ("-", "_"):
            self.angle_box.stepDown()
        elif key == "m":
            self.sound_action.toggle()
        elif key == "t":
            self.top_action.toggle()
        elif len(key) == 1 and key.isdigit():
            self.opacity_box.setCurrentIndex(int(key) - 1 if key != "0" else 9)
        else:
            super().keyPressEvent(event)

    def tick(self):
        ok, frame = self.cap.read()
        if not ok:
            self.bad_frames += 1
            if self.bad_frames > 50:
                print("The camera stopped sending pictures.")
                self.close()
            time.sleep(0.05)
            return
        self.bad_frames = 0

        now = time.monotonic()
        status, color, rel, alarming = self.monitor.update(self.tracker.head_yaw(frame, now), now)
        if alarming:
            self.alarm.ring(now)
        self.frame = cv2.flip(frame, 1)  # mirror, like a video call self-view
        self.view.show_frame(self.frame, status, color, rel, alarming, self.monitor.angle)

    def closeEvent(self, event):
        self.timer.stop()
        self.cap.release()
        self.tracker.close()
        super().closeEvent(event)


def main():
    args = parse_args()
    tracker = FaceTracker()
    alarm = Alarm(enabled=not args.mute)
    cap = open_camera(args.camera)

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in the terminal quits
    app = QApplication(sys.argv[:1])
    app.setApplicationName(WINDOW)
    window = MainWindow(args, cap, tracker, alarm)
    window.show()
    QTimer.singleShot(150, window.center_on_screen)  # once the window frame size is known
    if not args.no_top:
        window.show_on_all_workspaces()

    print("Look straight at the screen for 2 seconds to calibrate. Press q in the window to quit.")
    app.exec()


if __name__ == "__main__":
    main()
