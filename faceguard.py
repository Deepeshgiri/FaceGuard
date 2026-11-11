"""
FaceGuard - hides your screen when you are not looking at it.

Uses the webcam + OpenCV YuNet face detector (5 facial landmarks) to estimate
whether your head is facing the screen.

Features
  * Blur / black out / monitor off when you look away
  * Lock Windows after a longer absence
  * Shoulder-surfer detection (a second face behind you hides the screen)
  * Pauses your media when you look away, resumes when you come back
  * Steps aside during video calls (camera in use) and fullscreen apps
  * Break reminders (20-20-20) and daily screen-time stats
  * System tray icon, start with Windows

Global hotkey: Ctrl+Alt+F -> pause / resume protection
Command line:  --tray  start hidden in the tray
"""
import os
import sys

# If started with the system Python (e.g. `python faceguard.py`), relaunch with the
# project's virtual environment, which has OpenCV & co. installed.
_VENV_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "Scripts", "python.exe")
if os.path.exists(_VENV_PY) and os.path.normcase(sys.prefix) != os.path.normcase(os.path.dirname(os.path.dirname(_VENV_PY))):
    import subprocess
    sys.exit(subprocess.call([_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]]))

import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg
from tkinter import ttk

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageGrab, ImageTk

APP_NAME = "FaceGuard"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(APP_DIR, "models", "face_detection_yunet_2023mar.onnx")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATS_PATH = os.path.join(APP_DIR, "stats.json")

DEFAULTS = {
    "mode": "blur",              # blur | black | monitor_off
    "grace_seconds": 0.25,       # look away this long before hiding
    "restore_seconds": 0.2,      # look back this long before restoring
    "sensitivity": 1.0,          # >1 = stricter
    "camera_index": 0,
    "baseline_yaw": 0.0,
    "baseline_pitch": 0.55,
    "calibrated": False,
    "lock_enabled": True,
    "lock_after_seconds": 60,
    "intruder_enabled": True,
    "intruder_min_size": 0.35,   # extra faces at least this fraction of your face width
    "media_enabled": True,
    "pause_in_calls": True,
    "pause_fullscreen": True,
    "break_enabled": True,
    "break_minutes": 20,
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# ----------------------------------------------------------------- persistence
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(_load_json(CONFIG_PATH, {}))
    return cfg


def save_config(cfg):
    _save_json(CONFIG_PATH, cfg)


def fmt_duration(sec):
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ----------------------------------------------------------------- Win32 helpers
HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MOUSEEVENTF_MOVE = 0x0001
KEYEVENTF_KEYUP = 0x0002
VK_MEDIA_PLAY_PAUSE = 0xB3
ES_DISPLAY_REQUIRED = 0x00000002
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008


def monitor_off():
    user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)


def monitor_on():
    user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, -1)
    user32.mouse_event(MOUSEEVENTF_MOVE, 0, 1, 0, 0)
    user32.mouse_event(MOUSEEVENTF_MOVE, 0, -1, 0, 0)
    kernel32.SetThreadExecutionState(ES_DISPLAY_REQUIRED)


def lock_workstation():
    user32.LockWorkStation()


def press_media_play_pause():
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)


def virtual_screen():
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def fullscreen_app_running():
    """True when a fullscreen app / game / presentation is in the foreground."""
    state = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except Exception:
        return False
    # 2 = QUNS_BUSY (fullscreen), 3 = D3D fullscreen, 4 = presentation mode
    return state.value in (2, 3, 4)


_WEBCAM_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"


def other_app_using_camera():
    """Name of another app currently using the webcam (Zoom, Teams, ...), or None."""
    def scan(key_path):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            return None
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if sub == "NonPackaged":
                    hit = scan(key_path + "\\NonPackaged")
                    if hit:
                        return hit
                    continue
                name = sub.replace("#", "\\").split("\\")[-1]
                if name.lower().startswith("python"):
                    continue  # that's us
                try:
                    with winreg.OpenKey(key, sub) as sk:
                        start, _ = winreg.QueryValueEx(sk, "LastUsedTimeStart")
                        stop, _ = winreg.QueryValueEx(sk, "LastUsedTimeStop")
                    if start and stop == 0:
                        return name.split("_")[0]
                except OSError:
                    pass
        finally:
            winreg.CloseKey(key)
        return None
    return scan(_WEBCAM_KEY)


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command():
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{os.path.abspath(__file__)}" --tray'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def ensure_single_instance():
    kernel32.CreateMutexW(None, False, "Global\\FaceGuardSingleInstance")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# ----------------------------------------------------------------- audio
class AudioMeter:
    """Reports whether anything is currently playing sound."""

    def __init__(self):
        self.meter = None
        try:
            from ctypes import POINTER, cast
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            sp = AudioUtilities.GetSpeakers()
            dev = getattr(sp, "_dev", sp)
            iface = dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
            self.meter = cast(iface, POINTER(IAudioMeterInformation))
        except Exception:
            self.meter = None
        self.last_sound = 0.0

    def poll(self):
        if self.meter is None:
            return
        try:
            if self.meter.GetPeakValue() > 0.005:
                self.last_sound = time.time()
        except Exception:
            pass

    def playing(self, within=1.5):
        return time.time() - self.last_sound < within


# ----------------------------------------------------------------- face tracking
class FaceTracker(threading.Thread):
    """Reads the webcam and publishes whether the user is looking at the screen."""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.lock = threading.Lock()
        self.looking = False
        self.face_found = False
        self.extra_faces = 0
        self.yaw = self.pitch = 0.0
        self.preview = None
        self.error = None
        self.camera_busy_by = None    # name of another app using the camera
        self.running = True
        self._calib_samples = None
        self.detector = None
        self.haar = None
        self._cap = None
        self._last_busy_check = 0.0

    # -- calibration
    def start_calibration(self):
        with self.lock:
            self._calib_samples = []

    @property
    def calibrating(self):
        return self._calib_samples is not None

    # -- camera handling
    def _open_camera(self):
        idx = self.cfg["camera_index"]
        # MSMF goes through the Windows Frame Server, which lets other apps share the camera
        for backend in (cv2.CAP_MSMF, cv2.CAP_DSHOW, cv2.CAP_ANY):
            cap = cv2.VideoCapture(idx, backend)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # always process the newest frame
                ok, _ = cap.read()
                if ok:
                    return cap
            cap.release()
        return None

    def _close_camera(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _init_detector(self, w, h):
        if os.path.exists(MODEL_PATH) and hasattr(cv2, "FaceDetectorYN"):
            self.detector = cv2.FaceDetectorYN.create(MODEL_PATH, "", (w, h), 0.7, 0.3, 5000)
        else:
            self.haar = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def run(self):
        while self.running:
            t0 = time.time()

            # Step aside while a video call / other app uses the camera
            if self.cfg.get("pause_in_calls") and t0 - self._last_busy_check > 2.0:
                self._last_busy_check = t0
                busy = other_app_using_camera()
                self.camera_busy_by = busy
                if busy:
                    self._close_camera()
            elif not self.cfg.get("pause_in_calls"):
                self.camera_busy_by = None
            if self.camera_busy_by:
                time.sleep(0.5)
                continue

            if self._cap is None:
                self._cap = self._open_camera()
                if self._cap is None:
                    self.error = "Could not open webcam"
                    time.sleep(2)
                    continue

            ok, frame = self._cap.read()
            if not ok:
                self.error = "Webcam read failed"
                self._close_camera()
                time.sleep(1)
                continue
            self.error = None
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (320, int(h * 320.0 / w)))
            if self.detector is None and self.haar is None:
                self._init_detector(small.shape[1], small.shape[0])
            self._process(small)
            dt = time.time() - t0
            if dt < 0.04:            # ~25 fps
                time.sleep(0.04 - dt)
        self._close_camera()

    def _process(self, img):
        looking = found = False
        extra = 0
        yaw = pitch = 0.0
        vis = img.copy()

        if self.detector is not None:
            self.detector.setInputSize((img.shape[1], img.shape[0]))
            _, faces = self.detector.detect(img)
            if faces is not None and len(faces):
                faces = sorted(faces, key=lambda r: r[2] * r[3], reverse=True)
                f = faces[0]          # largest face = you
                found = True
                x, y, fw, fh = f[:4].astype(int)
                re, le, nose = f[4:6], f[6:8], f[8:10]
                rm, lm = f[10:12], f[12:14]
                eye_mid = (re + le) / 2
                mouth_mid = (rm + lm) / 2
                eye_dist = max(np.linalg.norm(le - re), 1e-3)
                vert = max(mouth_mid[1] - eye_mid[1], 1e-3)
                yaw = float((nose[0] - eye_mid[0]) / eye_dist)
                pitch = float((nose[1] - eye_mid[1]) / vert)

                with self.lock:
                    if self._calib_samples is not None:
                        self._calib_samples.append((yaw, pitch))
                        if len(self._calib_samples) >= 25:
                            ys, ps = zip(*self._calib_samples)
                            self.cfg["baseline_yaw"] = float(np.median(ys))
                            self.cfg["baseline_pitch"] = float(np.median(ps))
                            self.cfg["calibrated"] = True
                            save_config(self.cfg)
                            self._calib_samples = None

                s = max(self.cfg["sensitivity"], 0.1)
                looking = (abs(yaw - self.cfg["baseline_yaw"]) < 0.28 / s and
                           abs(pitch - self.cfg["baseline_pitch"]) < 0.20 / s)

                color = (0, 200, 0) if looking else (0, 0, 255)
                cv2.rectangle(vis, (x, y), (x + fw, y + fh), color, 2)
                for p in (re, le, nose, rm, lm):
                    cv2.circle(vis, (int(p[0]), int(p[1])), 2, (255, 200, 0), -1)

                # Other people looking at the screen
                min_w = fw * self.cfg["intruder_min_size"]
                for o in faces[1:]:
                    if o[2] >= min_w and o[14] >= 0.8:
                        extra += 1
                        ox, oy, ow, oh = o[:4].astype(int)
                        cv2.rectangle(vis, (ox, oy), (ox + ow, oy + oh), (0, 140, 255), 2)
        else:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = self.haar.detectMultiScale(gray, 1.15, 5, minSize=(50, 50))
            if len(faces):
                found = looking = True
                extra = len(faces) - 1
                x, y, fw, fh = max(faces, key=lambda r: r[2] * r[3])
                cv2.rectangle(vis, (x, y), (x + fw, y + fh), (0, 200, 0), 2)

        with self.lock:
            self.looking, self.face_found, self.extra_faces = looking, found, extra
            self.yaw, self.pitch = yaw, pitch
            self.preview = vis

    def snapshot(self):
        with self.lock:
            return (self.looking, self.face_found, self.extra_faces,
                    self.yaw, self.pitch, self.preview)


# ----------------------------------------------------------------- blur pre-render
class BlurCache(threading.Thread):
    """Keeps a recent blurred screenshot ready so the blur can appear instantly."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.lock = threading.Lock()
        self.image = None        # full-size blurred PIL image
        self.version = 0
        self._last_sig = None

    def run(self):
        while not getattr(self.app, "_quitting", False):
            if self.app.wants_blur_cache():
                try:
                    self.image_now()
                except Exception:
                    pass
            time.sleep(1.0)

    def image_now(self):
        vx, vy, vw, vh = virtual_screen()
        shot = ImageGrab.grab(all_screens=True)
        small = shot.reduce(8)
        sig = (vw, vh, small.resize((32, 18)).tobytes())
        if sig == self._last_sig:
            return
        self._last_sig = sig
        blurred = small.filter(ImageFilter.GaussianBlur(5)).resize((vw, vh), Image.BILINEAR)
        with self.lock:
            self.image = blurred
            self.version += 1

    def latest(self):
        with self.lock:
            return self.version, self.image


# ----------------------------------------------------------------- global hotkey
class HotkeyThread(threading.Thread):
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
    WM_HOTKEY = 0x0312

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback

    def run(self):
        if not user32.RegisterHotKey(None, 1, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, ord("F")):
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self.WM_HOTKEY:
                self.callback()


# ----------------------------------------------------------------- tray icon
def make_icon_image(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 16, 60, 48), fill=(255, 255, 255, 255), outline=color, width=4)
    d.ellipse((22, 22, 42, 42), fill=color)
    d.ellipse((28, 28, 36, 36), fill=(20, 20, 20, 255))
    return img


class Tray:
    def __init__(self, app):
        import pystray
        self.pystray = pystray
        self.app = app
        self.images = {
            "on": make_icon_image((30, 160, 60, 255)),
            "paused": make_icon_image((150, 150, 150, 255)),
            "hidden": make_icon_image((210, 60, 50, 255)),
        }
        M = pystray.MenuItem
        self.icon = pystray.Icon(
            APP_NAME, self.images["on"], APP_NAME,
            menu=pystray.Menu(
                M("Open FaceGuard", lambda: app.post("show_window"), default=True),
                M("Paused", lambda: app.post("toggle_pause"), checked=lambda _: app.paused),
                M("Calibrate", lambda: app.post("calibrate")),
                M(lambda _: f"Screen time today: {fmt_duration(app.today_seconds())}",
                  None, enabled=False),
                pystray.Menu.SEPARATOR,
                M("Quit", lambda: app.post("quit")),
            ))
        self._state = "on"
        threading.Thread(target=self.icon.run, daemon=True).start()

    def set_state(self, state):
        if state != self._state:
            self._state = state
            try:
                self.icon.icon = self.images[state]
            except Exception:
                pass

    def notify(self, message, title=APP_NAME):
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


# ----------------------------------------------------------------- app
class App:
    def __init__(self, start_hidden=False):
        self.cfg = load_config()
        self.tracker = FaceTracker(self.cfg)
        self.tracker.start()
        self.audio = AudioMeter()
        self.queue = queue.Queue()

        self.hidden = False
        self.hide_reason = None
        self.paused = False
        self.away_since = None
        self.back_since = None
        self.intruder_since = None
        self.locked_this_absence = False
        self.media_paused_by_us = False
        self.fullscreen = False
        self._last_fs_check = 0.0

        # screen time / breaks
        self.stats = _load_json(STATS_PATH, {})
        self.continuous_screen = 0.0
        self.away_streak = 0.0
        self._last_tick = time.time()
        self._last_stats_save = time.time()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._build_ui()
        self._build_overlay()
        self.blur_cache = BlurCache(self)
        self.blur_cache.start()

        try:
            self.tray = Tray(self)
        except Exception:
            self.tray = None
            self.root.protocol("WM_DELETE_WINDOW", self.quit)

        HotkeyThread(lambda: self.post("toggle_pause")).start()

        if start_hidden and self.tray:
            self.root.withdraw()
        if not self.cfg.get("calibrated"):
            self.root.after(1500, self.calibrate)
        self.root.after(100, self.tick)

    # ---- thread-safe command queue (tray + hotkey run on other threads)
    def post(self, cmd):
        self.queue.put(cmd)

    def _drain_queue(self):
        while True:
            try:
                cmd = self.queue.get_nowait()
            except queue.Empty:
                return
            if cmd == "toggle_pause":
                self.toggle_pause()
            elif cmd == "show_window":
                self.show_window()
            elif cmd == "calibrate":
                self.show_window()
                self.calibrate()
            elif cmd == "quit":
                self.quit()
                return

    # ---- UI
    def _build_ui(self):
        pad = {"padx": 8, "pady": 3}
        nb = ttk.Notebook(self.root)
        nb.pack(padx=8, pady=8)

        # Status tab
        st = ttk.Frame(nb, padding=8)
        nb.add(st, text="Status")
        self.preview_lbl = ttk.Label(st)
        self.preview_lbl.pack(**pad)
        self.status_var = tk.StringVar(value="Starting camera...")
        ttk.Label(st, textvariable=self.status_var, font=("Segoe UI", 11, "bold"),
                  wraplength=340, justify="center").pack(**pad)
        self.time_var = tk.StringVar()
        ttk.Label(st, textvariable=self.time_var, foreground="#555").pack(**pad)
        bf = ttk.Frame(st)
        bf.pack(pady=6)
        ttk.Button(bf, text="Calibrate (look at screen)", command=self.calibrate).pack(side="left", padx=4)
        self.pause_btn = ttk.Button(bf, text="Pause", command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=4)
        ttk.Label(st, text="Ctrl+Alt+F = pause/resume from anywhere", foreground="#666").pack()

        # Settings tab
        sf = ttk.Frame(nb, padding=8)
        nb.add(sf, text="Settings")
        r = 0
        ttk.Label(sf, text="When I look away:").grid(row=r, column=0, sticky="w", **pad)
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        mf = ttk.Frame(sf)
        mf.grid(row=r, column=1, sticky="w")
        for val, txt in (("blur", "Blur"), ("black", "Black"), ("monitor_off", "Monitor off")):
            ttk.Radiobutton(mf, text=txt, value=val, variable=self.mode_var,
                            command=self._save_settings).pack(side="left", padx=2)
        r += 1
        self.grace_var = self._spin(sf, r, "Hide after (seconds away):", "grace_seconds", 0.0, 30, 0.25)
        r += 1
        ttk.Label(sf, text="Strictness:").grid(row=r, column=0, sticky="w", **pad)
        self.sens_var = tk.DoubleVar(value=self.cfg["sensitivity"])
        ttk.Scale(sf, from_=0.5, to=2.5, variable=self.sens_var, length=170,
                  command=lambda _=None: self._save_settings()).grid(row=r, column=1, sticky="w", **pad)
        r += 1
        ttk.Separator(sf).grid(row=r, column=0, columnspan=2, sticky="ew", pady=6)
        r += 1

        self.lock_var = self._check(sf, r, "Lock Windows after I'm away for", "lock_enabled")
        self.lock_secs_var = tk.IntVar(value=int(self.cfg["lock_after_seconds"]))
        lf = ttk.Frame(sf)
        lf.grid(row=r, column=1, sticky="w")
        ttk.Spinbox(lf, from_=10, to=3600, increment=10, width=6, textvariable=self.lock_secs_var,
                    command=self._save_settings).pack(side="left")
        ttk.Label(lf, text=" seconds").pack(side="left")
        r += 1
        self.intruder_var = self._check(sf, r, "Hide screen if someone else is looking", "intruder_enabled", span=2)
        r += 1
        self.media_var = self._check(sf, r, "Pause media when I look away", "media_enabled", span=2)
        r += 1
        self.calls_var = self._check(sf, r, "Step aside during video calls (camera in use)", "pause_in_calls", span=2)
        r += 1
        self.fs_var = self._check(sf, r, "Step aside during fullscreen apps / videos", "pause_fullscreen", span=2)
        r += 1
        self.break_var = self._check(sf, r, "Break reminder every", "break_enabled")
        self.break_min_var = tk.IntVar(value=int(self.cfg["break_minutes"]))
        bf2 = ttk.Frame(sf)
        bf2.grid(row=r, column=1, sticky="w")
        ttk.Spinbox(bf2, from_=5, to=180, increment=5, width=6, textvariable=self.break_min_var,
                    command=self._save_settings).pack(side="left")
        ttk.Label(bf2, text=" minutes of screen time").pack(side="left")
        r += 1
        ttk.Separator(sf).grid(row=r, column=0, columnspan=2, sticky="ew", pady=6)
        r += 1
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(sf, text="Start FaceGuard with Windows (in tray)", variable=self.autostart_var,
                        command=self._toggle_autostart).grid(row=r, column=0, columnspan=2, sticky="w", **pad)

        for w in (self.grace_var, self.lock_secs_var, self.break_min_var):
            w.trace_add("write", lambda *_: self._save_settings())

    def _spin(self, parent, row, label, key, lo, hi, inc):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=3)
        var = tk.DoubleVar(value=self.cfg[key])
        ttk.Spinbox(parent, from_=lo, to=hi, increment=inc, width=6, textvariable=var,
                    command=self._save_settings).grid(row=row, column=1, sticky="w", padx=8, pady=3)
        return var

    def _check(self, parent, row, label, key, span=1):
        var = tk.BooleanVar(value=bool(self.cfg[key]))
        ttk.Checkbutton(parent, text=label, variable=var, command=self._save_settings).grid(
            row=row, column=0, columnspan=span, sticky="w", padx=8, pady=3)
        return var

    def _save_settings(self):
        try:
            self.cfg["mode"] = self.mode_var.get()
            self.cfg["grace_seconds"] = max(0.0, float(self.grace_var.get()))
            self.cfg["sensitivity"] = round(float(self.sens_var.get()), 2)
            self.cfg["lock_enabled"] = self.lock_var.get()
            self.cfg["lock_after_seconds"] = max(10, int(self.lock_secs_var.get()))
            self.cfg["intruder_enabled"] = self.intruder_var.get()
            self.cfg["media_enabled"] = self.media_var.get()
            self.cfg["pause_in_calls"] = self.calls_var.get()
            self.cfg["pause_fullscreen"] = self.fs_var.get()
            self.cfg["break_enabled"] = self.break_var.get()
            self.cfg["break_minutes"] = max(1, int(self.break_min_var.get()))
        except (tk.TclError, ValueError):
            return
        save_config(self.cfg)

    def _toggle_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
        except OSError:
            self.autostart_var.set(autostart_enabled())

    # ---- window / pause
    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self):
        self.root.withdraw()
        if self.tray and not getattr(self, "_told_tray", False):
            self._told_tray = True
            self.tray.notify("Still running in the tray. Right-click the eye icon to quit.")

    def calibrate(self):
        self.tracker.start_calibration()

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.config(text="Resume" if self.paused else "Pause")
        if self.paused:
            self.show_screen()

    # ---- hide / show
    def _build_overlay(self):
        ov = tk.Toplevel(self.root)
        ov.withdraw()
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.configure(bg="black")
        self.overlay_label = tk.Label(ov, bd=0, bg="black")
        self.overlay_label.pack(fill="both", expand=True)
        ov.update_idletasks()
        try:  # never steal keyboard focus, no taskbar button
            hwnd = user32.GetParent(ov.winfo_id()) or ov.winfo_id()
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        except Exception:
            pass
        self.overlay = ov
        self.overlay_photo = None
        self._photo_version = -1

    def wants_blur_cache(self):
        return not self.hidden and self.cfg["mode"] == "blur" and not self.paused

    def _refresh_overlay_photo(self):
        """Convert the newest pre-rendered blur into a Tk image (main thread only)."""
        version, img = self.blur_cache.latest()
        if img is not None and version != self._photo_version:
            self._photo_version = version
            self.overlay_photo = ImageTk.PhotoImage(img)

    def hide_screen(self, reason):
        if self.hidden:
            return
        self.hidden = True
        self.hide_reason = reason
        mode = self.cfg["mode"]
        self._hidden_mode = mode
        if mode == "monitor_off":
            monitor_off()
        else:
            if mode == "blur":
                if self.overlay_photo is None:       # first time: render synchronously
                    try:
                        self.blur_cache.image_now()
                    except Exception:
                        pass
                    self._refresh_overlay_photo()
                self.overlay_label.configure(image=self.overlay_photo or "")
            else:
                self.overlay_label.configure(image="")
            vx, vy, vw, vh = virtual_screen()
            prev = user32.GetForegroundWindow()
            self.overlay.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
            self.overlay.deiconify()
            self.overlay.lift()
            self.overlay.attributes("-topmost", True)
            self.overlay.update_idletasks()
            if prev:
                user32.SetForegroundWindow(prev)    # give keyboard focus back
        if self.cfg["media_enabled"] and reason == "away" and self.audio.playing():
            press_media_play_pause()
            self.media_paused_by_us = True

    def show_screen(self):
        if not self.hidden:
            return
        self.hidden = False
        self.hide_reason = None
        self.overlay.withdraw()
        if getattr(self, "_hidden_mode", None) == "monitor_off":
            monitor_on()
        if self.media_paused_by_us:
            self.media_paused_by_us = False
            press_media_play_pause()

    # ---- stats / breaks
    def today_seconds(self):
        return self.stats.get(datetime.date.today().isoformat(), 0)

    def _update_screen_time(self, present, dt):
        if present:
            day = datetime.date.today().isoformat()
            self.stats[day] = self.stats.get(day, 0) + dt
            self.continuous_screen += dt
            self.away_streak = 0.0
        else:
            self.away_streak += dt
            if self.away_streak >= 20:        # a 20 s break resets the clock
                self.continuous_screen = 0.0

        limit = self.cfg["break_minutes"] * 60
        if self.cfg["break_enabled"] and self.continuous_screen >= limit:
            if self.tray:
                self.tray.notify(
                    f"You've been at the screen for {self.cfg['break_minutes']} minutes. "
                    "Look at something ~6 m (20 ft) away for 20 seconds.", "Time for a break")
            # remind again in 5 minutes if no break is taken
            self.continuous_screen = max(0.0, limit - 300)

        if time.time() - self._last_stats_save > 60:
            self._last_stats_save = time.time()
            _save_json(STATS_PATH, self.stats)

    # ---- main loop
    def tick(self):
        self._drain_queue()
        if getattr(self, "_quitting", False):
            return
        now = time.time()
        dt = min(now - self._last_tick, 1.0)
        self._last_tick = now

        looking, found, extra, yaw, pitch, preview = self.tracker.snapshot()
        self.audio.poll()

        if not self.hidden and now - self._last_fs_check > 1.0:
            self._last_fs_check = now
            self.fullscreen = self.cfg["pause_fullscreen"] and fullscreen_app_running()

        busy = self.tracker.camera_busy_by
        stepped_aside = None
        if self.paused:
            stepped_aside = "Paused"
        elif busy:
            stepped_aside = f"Stepped aside: camera in use by {busy}"
        elif self.fullscreen:
            stepped_aside = "Stepped aside: fullscreen app"
        elif self.tracker.error:
            stepped_aside = self.tracker.error
        elif self.tracker.calibrating:
            stepped_aside = "Calibrating... look straight at your screen"

        # status text
        if stepped_aside:
            status = stepped_aside
        elif not found:
            status = "No face detected"
        else:
            status = ("Looking at screen" if looking else "Looking away") + \
                     f"   (yaw {yaw - self.cfg['baseline_yaw']:+.2f}, pitch {pitch - self.cfg['baseline_pitch']:+.2f})"
            if extra and self.cfg["intruder_enabled"]:
                status = f"Someone else is looking! ({extra} extra face{'s' if extra > 1 else ''})"
        self.status_var.set(status)
        self.time_var.set(f"Screen time today: {fmt_duration(self.today_seconds())}")

        if preview is not None and not self.hidden and self.root.state() != "withdrawn":
            rgb = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)
            self._pimg = ImageTk.PhotoImage(Image.fromarray(rgb))
            self.preview_lbl.configure(image=self._pimg)

        # decide
        if stepped_aside:
            self.away_since = self.back_since = self.intruder_since = None
            if self.hidden and not self.tracker.calibrating:
                self.show_screen()
            self._update_screen_time(bool(busy or self.fullscreen), dt)
        else:
            intruder = self.cfg["intruder_enabled"] and extra > 0
            if intruder:
                self.intruder_since = self.intruder_since or now
            else:
                self.intruder_since = None

            if looking:
                self.away_since = None
                self.locked_this_absence = False
            else:
                self.away_since = self.away_since or now

            want_hidden = (
                (self.intruder_since and now - self.intruder_since >= 0.5) or
                (self.away_since and now - self.away_since >= self.cfg["grace_seconds"])
            )
            if want_hidden:
                self.back_since = None
                if not self.hidden:
                    self.hide_screen("intruder" if self.intruder_since and looking else "away")
            elif self.hidden:
                self.back_since = self.back_since or now
                if now - self.back_since >= self.cfg["restore_seconds"]:
                    self.show_screen()
                    self.back_since = None

            if (self.cfg["lock_enabled"] and self.away_since and not self.locked_this_absence
                    and now - self.away_since >= self.cfg["lock_after_seconds"]):
                self.locked_this_absence = True
                lock_workstation()

            self._update_screen_time(looking, dt)

        if self.tray:
            self.tray.set_state("paused" if stepped_aside else "hidden" if self.hidden else "on")

        if self.hidden and self._hidden_mode != "monitor_off":
            self._overlay_keepalive = getattr(self, "_overlay_keepalive", 0) + 1
            if self._overlay_keepalive % 15 == 0:   # stay above other topmost windows
                self.overlay.lift()
                self.overlay.attributes("-topmost", True)
        elif not self.hidden and self.cfg["mode"] == "blur":
            self._refresh_overlay_photo()

        self.root.after(30, self.tick)

    def quit(self):
        self._quitting = True
        self.tracker.running = False
        self.show_screen()
        _save_json(STATS_PATH, self.stats)
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if not ensure_single_instance():
        sys.exit(0)
    App(start_hidden="--tray" in sys.argv).run()
