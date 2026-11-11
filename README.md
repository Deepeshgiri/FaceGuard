# FaceGuard

**Your screen, only while you're looking at it.**

FaceGuard is a small Windows app that uses your webcam to tell whether you are facing the screen. When you look away, it blurs the screen, blacks it out or turns the monitor off. When you look back, your screen returns.

Everything runs locally on your PC. No images are saved and nothing is sent anywhere.

---

## Features

| Feature | What it does |
| --- | --- |
| **Hide when you look away** | Blurs or blacks out the screen, or turns the monitor off, about 0.25 s after you turn your head away or leave. |
| **Auto-lock** | Locks Windows (like `Win + L`) after you've been away for a while (default 60 s). |
| **Shoulder-surfer detection** | Hides the screen when a second person appears in view, even while you are looking. |
| **Media pause** | Pauses music or video when you look away and resumes it when you return. |
| **Video call aware** | Steps aside while Zoom, Teams, Discord or another app is using the camera. |
| **Fullscreen aware** | Steps aside during fullscreen videos, games and presentations (optional). |
| **Break reminders** | Reminds you to rest your eyes after 20 minutes of continuous screen time (20-20-20 rule). |
| **Screen-time stats** | Tracks how long you actually spend looking at the screen each day. |
| **Tray icon** | Runs quietly in the system tray. Can start with Windows. |

## Requirements

- Windows 10 or 11
- A webcam
- Python 3.10 or newer

## Installation

```powershell
git clone <your-repo-url> FaceGuard
cd FaceGuard
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The face detection model (`models/face_detection_yunet_2023mar.onnx`) is included in the repository. If it is missing, download it from the
[OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) and place it in `models/`. Without it, FaceGuard falls back to a simpler detector that cannot tell which way your head is turned.

## Usage

Start FaceGuard with either:

- **Double-click `FaceGuard.bat`**, which runs it with no console window, or
- `python faceguard.py`. If this is not the project's Python, the script relaunches itself with the project's Python.

Starting with `--tray` opens it minimised to the tray.

### First run: calibration

On first launch, FaceGuard spends a few seconds calibrating. **Look straight at your screen** while it does. This records your natural head position relative to the webcam, so an off-centre camera works fine. To recalibrate later, use **Calibrate** in the window or the tray menu.

### Controls

| Action | How |
| --- | --- |
| Pause or resume protection | `Ctrl + Alt + F` (works from anywhere) |
| Open the window | Double-click the tray icon |
| Quit | Right-click the tray icon, then **Quit** |

Closing the window only hides it to the tray. FaceGuard keeps running.

The tray icon shows the current state:

- **Green**: protecting
- **Red**: screen is hidden
- **Grey**: paused or stepped aside

## Settings

All settings are in the **Settings** tab and are saved automatically.

| Setting | Default | Notes |
| --- | --- | --- |
| When I look away | Blur | `Blur`, `Black` or `Monitor off` |
| Hide after | 0.25 s | `0` is fastest but may flicker in poor lighting |
| Strictness | 1.0 | Move right so smaller head turns hide the screen |
| Lock Windows after | 60 s | |
| Hide if someone else is looking | On | |
| Pause media when I look away | On | |
| Step aside during video calls | On | |
| Step aside during fullscreen apps | On | |
| Break reminder | Every 20 min | A 20-second look-away counts as a break |
| Start with Windows | Off | Adds an entry under `HKCU\...\CurrentVersion\Run` |

## How it works

1. Each webcam frame is analysed with OpenCV's **YuNet** face detector, which finds your face and five landmarks: eyes, nose tip and mouth corners.
2. Head direction is estimated from where your nose sits relative to your eyes and mouth:
   - **Yaw** (left or right) is the nose's horizontal offset from the midpoint between your eyes, divided by the distance between your eyes.
   - **Pitch** (up or down) is how far the nose sits between eye level and mouth level.
3. If either value moves too far from your calibrated position, or no face is found, you count as looking away.
4. The blurred screen is prepared in the background, so it can appear instantly when needed.

## Limitations

- **It tracks head direction, not eye movement.** Glancing down at your phone without moving your head is not detected.
- **The blur is a privacy screen, not a security boundary.** Someone at your keyboard can still type into the app underneath. Auto-lock is what actually protects your PC.
- **Lighting matters.** In a dark room or with strong backlight, detection gets less reliable. Raise **Hide after** if the screen flickers.
- **Media pause** sends the system play/pause key, which goes to whichever player Windows treats as active. That may not be the app making the sound.
- **Monitor off** depends on your hardware. Some displays take a moment to wake, and a few ignore the command.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'cv2'` | Install the dependencies (see [Installation](#installation)). The package is `opencv-python`, not `cv2`. |
| "Could not open webcam" | Close other apps that use the camera, and check **Settings → Privacy & security → Camera** in Windows. |
| Screen hides while I'm looking | Recalibrate, or lower **Strictness**. |
| Screen doesn't hide when I turn away | Recalibrate, or raise **Strictness**. |
| Screen flickers | Raise **Hide after** to about 0.5 s, or improve the lighting. |
| Says "camera in use" when no call is running | Turn off **Step aside during video calls**. |

## Files

```
FaceGuard/
├── faceguard.py        # the app
├── FaceGuard.bat       # launcher (no console window)
├── requirements.txt
├── models/
│   └── face_detection_yunet_2023mar.onnx
├── config.json         # your settings (created at runtime)
└── stats.json          # daily screen time (created at runtime)
```

## Privacy

- Webcam frames are processed in memory and immediately discarded.
- Nothing is recorded, uploaded or logged.
- The only data stored is your settings and daily screen-time totals, kept in two local JSON files.

## Credits

- Face detection: [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) from the OpenCV Zoo (MIT License)
- Built with [OpenCV](https://opencv.org/), [Pillow](https://python-pillow.org/), [pystray](https://github.com/moses-palmer/pystray) and [pycaw](https://github.com/AndreMiras/pycaw)
