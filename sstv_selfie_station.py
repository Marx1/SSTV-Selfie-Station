#!/usr/bin/env python3
"""
SSTV Selfie Station -  3
=================================

 3 builds on the working  2 camera/display framework and adds:

  * Robot36 SSTV audio encoding at 48 kHz.
  * Receive-audio channel-clear detection through the AIOC.
  * Automatic wait-for-clear behavior using the radio's squelched audio.
  * Serial PTT control through the AIOC using DTR=1 / RTS=0.
  * Direct Robot36 playback to the automatically discovered AIOC ALSA device.
  * Safe PTT cleanup if playback fails or the process is interrupted.
"""

from __future__ import annotations

import glob
import logging
import math
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from array import array
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# ============================================================================
# USER CONFIGURATION
# ============================================================================

# Station identification placed on captured SSTV images.
CALLSIGN = "NOCALL"
EVENT_NAME = "SSTV Selfie Station"

# ---------------------------------------------------------------------------
# PiTFT
# ---------------------------------------------------------------------------

# Adafruit 3.5" resistive PiTFT, Product ID 2441.
TFT_ENABLE = True
TFT_WIDTH = 480
TFT_HEIGHT = 320
TFT_ROTATION = 270

# Display the live camera preview on the PiTFT.
TFT_PREVIEW_ENABLE = True

# PiTFT/SPI updates are intentionally limited so this remains usable on Pi 3B
# through Pi 5 without wasting CPU on display refreshes the TFT cannot use.
TFT_PREVIEW_FPS = 10.0

# After a selfie is triggered, hold the completed 320x240 image on the TFT for
# this many seconds before returning to live preview.
CAPTURE_DISPLAY_SECONDS = 3.0

# Keep Linux tty1 on HDMI rather than on the PiTFT. The framebuffer number is
# discovered dynamically from /proc/fb because fb0/fb1 ordering can change.
MAP_TTY1_TO_HDMI = True
HDMI_FRAMEBUFFER_NAME = "vc4drmfb"
LINUX_CONSOLE_NUMBER = 1
LINUX_CONSOLE_FIRST = 1
LINUX_CONSOLE_LAST = 6

# Driver identity used to recognize the Adafruit PiTFT framebuffer.
PITFT_FRAMEBUFFER_NAMES = (
    "fb_hx8357d",
    "fb_ili9486",
)

# ---------------------------------------------------------------------------
# Trigger configuration
# ---------------------------------------------------------------------------

KEYBOARD_TRIGGER_ENABLE = True
KEYBOARD_KEY = "KEY_B"

# Blank = accept KEYBOARD_KEY from any matching keyboard/HID input device.
KEYBOARD_DEVICE_MATCH = ""

# BCM GPIO numbering.
GPIO_TRIGGER_ENABLE = True
GPIO_PIN = 17
GPIO_PULL_UP = True
GPIO_BOUNCE_TIME = 0.10

# Prevent accidental duplicate triggers.
TRIGGER_LOCKOUT_SECONDS = 1.0

# ---------------------------------------------------------------------------
# Camera configuration
# ---------------------------------------------------------------------------

# Blank = automatically choose the first usable camera.
PREFERRED_CAMERA_NAME = ""

# Requested capture format. Camera drivers may choose the nearest supported
# mode. The final SSTV image is still generated at SSTV_WIDTH x SSTV_HEIGHT.
CAMERA_CAPTURE_WIDTH = 1280
CAMERA_CAPTURE_HEIGHT = 720
CAMERA_FPS = 10

# Favor newest frames over buffered/stale frames for a responsive preview.
CAMERA_LOW_LATENCY = True

# Raspberry Pi ISP pipeline nodes are not physical camera sources and must not
# be selected by the Selfie Station.
EXCLUDED_CAMERA_NAME_PREFIXES = (
    "bcm2835-isp-capture",
)

# ---------------------------------------------------------------------------
# SSTV image preparation
# ---------------------------------------------------------------------------

# Robot36 picture dimensions.
SSTV_WIDTH = 320
SSTV_HEIGHT = 240

# FIT   = preserve the complete frame and letterbox as necessary.
# CROP  = preserve aspect ratio but center-crop to fill 320x240.
# STRETCH = force the source into 320x240 regardless of aspect ratio.
IMAGE_RESIZE_MODE = "CROP"

# Background used by FIT mode where letterboxing is necessary.
IMAGE_BACKGROUND = (0, 0, 0)

# Final SSTV-image identification overlay.
#
# The live PiTFT preview remains clean with no overlay. The saved/transmitted
# 320x240 SSTV image uses a single-line caption with no full-width bar. The
# text is drawn directly on the image with a dark outline so the background
# remains visible everywhere except the words themselves.
OVERLAY_ENABLE = True
OVERLAY_FONT_SIZE = 14
OVERLAY_TEXT = (255, 255, 255)
OVERLAY_STROKE_COLOR = (0, 0, 0)
OVERLAY_STROKE_WIDTH = 2
OVERLAY_PADDING_X = 8
OVERLAY_PADDING_Y = 6

# PiTFT-only banner shown while the station is in dedicated transmit mode.
# This does NOT alter the actual 320x240 SSTV image being transmitted.
TRANSMIT_BANNER_ENABLE = True
TRANSMIT_BANNER_TEXT = "TRANSMITTING"
TRANSMIT_BANNER_FONT_SIZE = 36
TRANSMIT_BANNER_TEXT_COLOR = (255, 0, 0)
TRANSMIT_BANNER_STROKE_COLOR = (255, 255, 255)
TRANSMIT_BANNER_STROKE_WIDTH = 2
TRANSMIT_BANNER_PADDING_Y = 8
TRANSMIT_BANNER_BACKGROUND = (0, 0, 0)
TRANSMIT_BANNER_BACKGROUND_HEIGHT = 54

# Saved image location. Relative paths are resolved from the process working
# directory (/opt/sstv-selfie-station when using the supplied service).
CAPTURE_DIR = "captures"

# Keep timestamped JPEG copies of each transmitted selfie. The working
# latest_sstv.jpg file is always maintained because it is used by the
# transmitter, even when capture history is disabled.
SAVE_CAPTURES = True
LATEST_CAPTURE_FILENAME = "latest_sstv.jpg"
JPEG_QUALITY = 95

# ---------------------------------------------------------------------------
# AIOC discovery
# ---------------------------------------------------------------------------

AIOC_USB_VID = "1209"
AIOC_USB_PID = "7388"
AIOC_NAME_MATCHES = ("AIOC", "AllInOneCable", "All-in-one-Cable")
AIOC_AUDIO_RATE = 48000

# ---------------------------------------------------------------------------
# Robot36 / channel sensing / transmit
# ---------------------------------------------------------------------------

TRANSMIT_ENABLE = True

# Robot36 and the AIOC both use 48 kHz mono audio.
SSTV_SAMPLE_RATE = 48000
SSTV_BITS_PER_SAMPLE = 16

# Scale the generated SSTV audio before playback. 100 = full generated level.
# Start conservatively and adjust the AIOC/radio audio level as needed.
TX_AUDIO_LEVEL_PERCENT = 70

# Give the radio/AIOC time to settle after PTT is asserted before audio starts.
PTT_PREKEY_SECONDS = 0.25

# Hold PTT very briefly after the audio stream drains.
PTT_POSTKEY_SECONDS = 0.10

# Channel-clear detection uses the AIOC receive audio. With a squelched radio,
# silence should be well below this RMS threshold. If normal receiver noise is
# being treated as busy, make the threshold less negative (for example -35).
# If weak received audio is not detected, make it more negative (for example -50).
CHANNEL_BUSY_RMS_DBFS = -45.0

# Each channel check records this many whole seconds through arecord.
CHANNEL_CHECK_SECONDS = 1

# Number of consecutive clear checks required before PTT.
CHANNEL_CLEAR_CHECKS_REQUIRED = 1

# Delay before re-checking when the channel is busy.
CHANNEL_BUSY_RECHECK_SECONDS = 0.75

# Stop waiting after this long. Set to 0 to wait indefinitely.
CHANNEL_CLEAR_TIMEOUT_SECONDS = 60.0

# Normal transmission streams Robot36 directly to ALSA so PTT is not delayed
# by pre-rendering the entire WAV. Enable this only if a diagnostic WAV is wanted.
SAVE_SSTV_WAV = False
SSTV_WAV_FILENAME = "latest_sstv.wav"

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

HARDWARE_RESCAN_SECONDS = 5.0
CAMERA_REOPEN_SECONDS = 2.0
CAMERA_FRAME_STALE_SECONDS = 2.0

LOG_LEVEL = logging.INFO
LOG_FILE = "/var/log/sstv-selfie-station.log"


# ============================================================================
# DATA CLASSES / GLOBAL STATE
# ============================================================================

@dataclass(frozen=True)
class CameraDevice:
    backend: str
    device: str
    name: str


@dataclass(frozen=True)
class AIOCDevice:
    usb_path: str
    usb_name: str
    alsa_card: Optional[str] = None
    alsa_card_number: Optional[int] = None
    tty_device: Optional[str] = None


@dataclass(frozen=True)
class TFTDevice:
    enabled: bool
    detected: bool
    backend: Optional[str] = None
    device: Optional[str] = None
    description: Optional[str] = None


shutdown_event = threading.Event()
station_busy_event = threading.Event()
trigger_lock = threading.Lock()
capture_lock = threading.Lock()
tx_lock = threading.Lock()
state_lock = threading.Lock()
frame_lock = threading.Lock()
tft_hold_lock = threading.Lock()
tft_write_lock = threading.Lock()

last_trigger_time = 0.0

active_camera: Optional[CameraDevice] = None
active_tft: Optional[TFTDevice] = None
active_aioc: Optional[AIOCDevice] = None
latest_frame_rgb = None
latest_frame_time = 0.0

# While this timestamp is in the future, the live preview worker must not
# overwrite a just-captured image being displayed on the TFT.
tft_hold_until = 0.0

# Used to avoid repainting identical status pages every scan cycle.
_tft_last_screen: Optional[str] = None


# ============================================================================
# LOGGING / GENERAL HELPERS
# ============================================================================

def setup_logging() -> None:
    """Configure console logging and, when permitted, file logging."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    try:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    except (PermissionError, OSError):
        # Interactive non-root testing should still work.
        pass

    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=handlers,
    )


def run_command(command: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """Run a command and return (return_code, combined stdout/stderr)."""
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""


def read_text(path: str | Path) -> str:
    """Read a small sysfs/procfs text file without raising normal errors."""
    try:
        return Path(path).read_text(errors="replace").strip()
    except (OSError, PermissionError):
        return ""


def resolve_usb_parent(sys_path: str | Path) -> Optional[Path]:
    """Walk upward through sysfs until a physical USB device is found."""
    try:
        path = Path(sys_path).resolve()
    except OSError:
        return None

    for parent in (path, *path.parents):
        if (parent / "idVendor").exists() and (parent / "idProduct").exists():
            return parent
    return None


def camera_display_name(name: str) -> str:
    """Collapse exact repeated labels such as 'Cam Link 4K: Cam Link 4K'."""
    cleaned = " ".join(name.split()).strip()
    if ":" in cleaned:
        left, right = (part.strip() for part in cleaned.split(":", 1))
        if left and right and left.casefold() == right.casefold():
            return left
    return cleaned


# ============================================================================
# CAMERA DISCOVERY
# ============================================================================

def discover_v4l2_cameras() -> list[CameraDevice]:
    """Discover usable V4L2 capture devices."""
    cameras: list[CameraDevice] = []

    for device in sorted(glob.glob("/dev/video*")):
        sys_name = Path(device).name
        name = read_text(f"/sys/class/video4linux/{sys_name}/name") or sys_name
        is_capture = True

        if shutil.which("v4l2-ctl"):
            rc, output = run_command(["v4l2-ctl", "-D", "-d", device])
            if rc == 0:
                lower = output.lower()
                is_capture = (
                    "video capture" in lower
                    or "video capture multiplanar" in lower
                    or "video capture mplane" in lower
                )

        excluded = any(
            name.lower().startswith(prefix.lower())
            for prefix in EXCLUDED_CAMERA_NAME_PREFIXES
        )

        if excluded:
            logging.debug("Ignoring non-camera V4L2 node: %s (%s)", name, device)
            continue

        if is_capture:
            cameras.append(CameraDevice("v4l2", device, name))

    return cameras


def discover_libcamera_cameras() -> list[CameraDevice]:
    """Discover Raspberry Pi CSI/libcamera cameras."""
    tool = shutil.which("rpicam-hello") or shutil.which("libcamera-hello")
    if not tool:
        return []

    rc, output = run_command([tool, "--list-cameras"], timeout=8.0)
    if rc != 0 or not output:
        return []

    cameras: list[CameraDevice] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and stripped[0].isdigit() and " : " in stripped:
            index, description = stripped.split(" : ", 1)
            cameras.append(CameraDevice("libcamera", index.strip(), description.strip()))

    return cameras


def discover_cameras() -> list[CameraDevice]:
    """Return all currently detected camera sources."""
    return discover_v4l2_cameras() + discover_libcamera_cameras()


def select_camera(cameras: list[CameraDevice]) -> Optional[CameraDevice]:
    """Choose the preferred camera, otherwise the first usable camera."""
    if not cameras:
        return None

    if PREFERRED_CAMERA_NAME:
        wanted = PREFERRED_CAMERA_NAME.lower()
        for camera in cameras:
            if wanted in camera.name.lower():
                return camera

    return cameras[0]


# ============================================================================
# AIOC DISCOVERY
# ============================================================================

def usb_identity_matches(usb_path: Path) -> bool:
    """Return True when a USB sysfs node appears to be the configured AIOC."""
    vid = read_text(usb_path / "idVendor").lower()
    pid = read_text(usb_path / "idProduct").lower()
    product = read_text(usb_path / "product")
    manufacturer = read_text(usb_path / "manufacturer")
    combined = f"{manufacturer} {product}".lower()

    if vid == AIOC_USB_VID.lower() and pid == AIOC_USB_PID.lower():
        return True

    return any(name.lower() in combined for name in AIOC_NAME_MATCHES)


def discover_aioc() -> Optional[AIOCDevice]:
    """Find the AIOC and its ALSA + ttyACM interfaces dynamically."""
    matches: list[Path] = []

    for path_string in glob.glob("/sys/bus/usb/devices/*"):
        path = Path(path_string)
        if (path / "idVendor").exists() and usb_identity_matches(path):
            matches.append(path.resolve())

    if not matches:
        return None

    usb_path = sorted(matches)[0]
    manufacturer = read_text(usb_path / "manufacturer")
    product = read_text(usb_path / "product")
    usb_name = " ".join(x for x in (manufacturer, product) if x) or "AIOC"

    alsa_card = None
    alsa_card_number = None
    tty_device = None

    for card_path_string in sorted(glob.glob("/sys/class/sound/card[0-9]*")):
        card_path = Path(card_path_string)
        parent = resolve_usb_parent(card_path / "device")
        if parent and parent.resolve() == usb_path:
            try:
                alsa_card_number = int(card_path.name.replace("card", ""))
                alsa_card = f"hw:{alsa_card_number},0"
            except ValueError:
                pass
            break

    for tty_path_string in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        tty_path = Path(tty_path_string)
        parent = resolve_usb_parent(tty_path / "device")
        if parent and parent.resolve() == usb_path:
            tty_device = f"/dev/{tty_path.name}"
            break

    return AIOCDevice(
        usb_path=str(usb_path),
        usb_name=usb_name,
        alsa_card=alsa_card,
        alsa_card_number=alsa_card_number,
        tty_device=tty_device,
    )


# ============================================================================
# FRAMEBUFFER / TFT DISCOVERY AND OUTPUT
# ============================================================================

def framebuffer_number_by_name(driver_name: str) -> Optional[int]:
    """Return the /proc/fb number for an exact framebuffer driver name."""
    try:
        for line in Path("/proc/fb").read_text().splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip() == driver_name:
                return int(parts[0])
    except (OSError, ValueError):
        return None
    return None


def map_linux_console_to_hdmi() -> None:
    """
    Keep Linux virtual consoles off the PiTFT when HDMI is available.

    If vc4drmfb exists, tty1-tty6 are dynamically mapped to that framebuffer.
    If HDMI is absent, the framebuffer mapping is left unchanged but the tty1
    cursor is hidden so it cannot blink over the Selfie Station UI.
    """
    if not MAP_TTY1_TO_HDMI:
        return

    fb_number = framebuffer_number_by_name(HDMI_FRAMEBUFFER_NAME)

    if fb_number is None:
        logging.info(
            "HDMI framebuffer '%s' is not present; console mapping unchanged.",
            HDMI_FRAMEBUFFER_NAME,
        )
        return

    if not shutil.which("con2fbmap"):
        logging.warning(
            "con2fbmap not found; install the 'fbset' package to keep consoles on HDMI."
        )
        return

    mapped = 0
    for console_number in range(LINUX_CONSOLE_FIRST, LINUX_CONSOLE_LAST + 1):
        rc, output = run_command(
            ["con2fbmap", str(console_number), str(fb_number)]
        )
        if rc == 0:
            mapped += 1
        else:
            logging.warning(
                "Unable to map tty%d to HDMI framebuffer fb%d: %s",
                console_number,
                fb_number,
                output or "con2fbmap failed",
            )

    if mapped:
        logging.info(
            "Mapped tty%d-tty%d to HDMI framebuffer fb%d (%s).",
            LINUX_CONSOLE_FIRST,
            LINUX_CONSOLE_LAST,
            fb_number,
            HDMI_FRAMEBUFFER_NAME,
        )



def discover_tft() -> TFTDevice:
    """Detect the PiTFT without relying on fixed fb0/fb1 numbering."""
    if not TFT_ENABLE:
        return TFTDevice(False, False, description="disabled in configuration")

    expected_sizes = {
        f"{TFT_WIDTH},{TFT_HEIGHT}".lower(),
        f"{TFT_HEIGHT},{TFT_WIDTH}".lower(),
    }

    # Prefer the known PiTFT driver identity when present.
    for fb_path_string in sorted(glob.glob("/sys/class/graphics/fb*")):
        fb_path = Path(fb_path_string)
        name = read_text(fb_path / "name")
        virtual_size = read_text(fb_path / "virtual_size")

        identity_matches = name in PITFT_FRAMEBUFFER_NAMES
        size_matches = virtual_size.lower() in expected_sizes
        fallback_identity = any(
            token in name.lower() for token in ("pitft", "ili9486", "hx8357")
        )

        if identity_matches or fallback_identity or size_matches:
            return TFTDevice(
                True,
                True,
                backend="framebuffer",
                device=f"/dev/{fb_path.name}",
                description=f"{name} {virtual_size}".strip(),
            )

    # Basic DRM fallback for future/newer driver stacks.
    for connector_string in sorted(glob.glob("/sys/class/drm/card*-*")):
        connector = Path(connector_string)
        status = read_text(connector / "status").lower()
        modes = {
            line.strip().lower()
            for line in read_text(connector / "modes").splitlines()
            if line.strip()
        }
        if status and status != "connected":
            continue
        if modes.intersection({f"{TFT_WIDTH}x{TFT_HEIGHT}", f"{TFT_HEIGHT}x{TFT_WIDTH}"}):
            return TFTDevice(
                True,
                True,
                backend="drm",
                device=str(connector),
                description=f"{connector.name} modes={','.join(sorted(modes))}",
            )

    return TFTDevice(
        True,
        False,
        description=f"no {TFT_WIDTH}x{TFT_HEIGHT} PiTFT framebuffer/DRM device found",
    )


def framebuffer_bits_per_pixel(framebuffer_device: str) -> int:
    """Return framebuffer bits-per-pixel, defaulting to 16 for the PiTFT."""
    fb_name = Path(framebuffer_device).name
    value = read_text(f"/sys/class/graphics/{fb_name}/bits_per_pixel")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 16


def write_image_to_framebuffer(image, framebuffer_device: str) -> bool:
    """
    Write a Pillow RGB image directly to a framebuffer.

    NumPy is used only for efficient RGB565/BGRX framebuffer packing so live
    PiTFT preview remains practical on a Raspberry Pi 3B through Pi 5.
    """
    try:
        import numpy as np
    except ImportError:
        logging.error("NumPy is not installed; TFT output unavailable.")
        return False

    try:
        fb_name = Path(framebuffer_device).name
        virtual_size = read_text(f"/sys/class/graphics/{fb_name}/virtual_size")
        width, height = [int(x) for x in virtual_size.split(",", 1)]
        bpp = framebuffer_bits_per_pixel(framebuffer_device)

        prepared = image.resize((width, height)).convert("RGB")
        rgb = np.asarray(prepared, dtype=np.uint8)

        if bpp == 16:
            r = rgb[:, :, 0].astype(np.uint16)
            g = rgb[:, :, 1].astype(np.uint16)
            b = rgb[:, :, 2].astype(np.uint16)
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            raw = rgb565.astype("<u2", copy=False).tobytes()

        elif bpp == 32:
            bgra = np.empty((height, width, 4), dtype=np.uint8)
            bgra[:, :, 0] = rgb[:, :, 2]
            bgra[:, :, 1] = rgb[:, :, 1]
            bgra[:, :, 2] = rgb[:, :, 0]
            bgra[:, :, 3] = 0
            raw = bgra.tobytes()

        else:
            logging.warning(
                "Unsupported framebuffer depth %d bpp on %s.",
                bpp,
                framebuffer_device,
            )
            return False

        with open(framebuffer_device, "wb", buffering=0) as framebuffer:
            framebuffer.write(raw)
        return True

    except Exception as exc:
        logging.warning("Unable to draw TFT image: %s", exc)
        return False


# ============================================================================
# PIL IMAGE HELPERS / STATUS SCREENS
# ============================================================================

def load_font(size: int, bold: bool = False):
    """Load a predictable system font, falling back to Pillow's default."""
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default()


def build_status_image(title: str, subtitle: str, fault: bool):
    """Build a 480x320 station status screen. Fault mode uses SMPTE-style bars."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (TFT_WIDTH, TFT_HEIGHT), "black")
    draw = ImageDraw.Draw(image)

    if fault:
        # SMPTE-style 75% color bars: primary upper row, reverse/black middle
        # row, and lower I/Q + PLUGE-style black-level patches.
        upper_colors = (
            (191, 191, 191),
            (191, 191,   0),
            (  0, 191, 191),
            (  0, 191,   0),
            (191,   0, 191),
            (191,   0,   0),
            (  0,   0, 191),
        )
        middle_colors = (
            (  0,   0, 191),
            ( 19,  19,  19),
            (191,   0, 191),
            ( 19,  19,  19),
            (  0, 191, 191),
            ( 19,  19,  19),
            (191, 191, 191),
        )

        upper_h = int(TFT_HEIGHT * 0.67)
        middle_h = int(TFT_HEIGHT * 0.12)
        middle_y0 = upper_h
        lower_y0 = upper_h + middle_h

        for index, color in enumerate(upper_colors):
            x0 = round(index * TFT_WIDTH / 7)
            x1 = round((index + 1) * TFT_WIDTH / 7)
            draw.rectangle((x0, 0, x1, upper_h), fill=color)

        for index, color in enumerate(middle_colors):
            x0 = round(index * TFT_WIDTH / 7)
            x1 = round((index + 1) * TFT_WIDTH / 7)
            draw.rectangle((x0, middle_y0, x1, lower_y0), fill=color)

        lower_blocks = (
            (  0,  33,  76),
            (255, 255, 255),
            ( 50,   0, 106),
            ( 19,  19,  19),
        )
        block_w = TFT_WIDTH // 4
        for index, color in enumerate(lower_blocks):
            x0 = index * block_w
            x1 = TFT_WIDTH if index == 3 else (index + 1) * block_w
            draw.rectangle((x0, lower_y0, x1, TFT_HEIGHT), fill=color)

        pluge_y0 = lower_y0 + int((TFT_HEIGHT - lower_y0) * 0.42)
        pluge_x0 = int(TFT_WIDTH * 0.72)
        pluge_width = int(TFT_WIDTH * 0.20)
        patch_w = max(1, pluge_width // 3)
        for index, color in enumerate(((7, 7, 7), (19, 19, 19), (31, 31, 31))):
            x0 = pluge_x0 + index * patch_w
            x1 = pluge_x0 + (index + 1) * patch_w
            draw.rectangle((x0, pluge_y0, x1, TFT_HEIGHT), fill=color)

        # Keep the error text readable while preserving the recognizable bars.
        band_top = int(TFT_HEIGHT * 0.36)
        band_bottom = int(TFT_HEIGHT * 0.62)
        draw.rectangle((0, band_top, TFT_WIDTH, band_bottom), fill=(0, 0, 0))

    title_font = load_font(42, bold=True)
    subtitle_font = load_font(22, bold=False)

    def centered(text: str, y: int, font) -> None:
        box = draw.textbbox((0, 0), text, font=font)
        text_width = box[2] - box[0]
        draw.text(((TFT_WIDTH - text_width) // 2, y), text, font=font, fill="white")

    if fault:
        centered(title, int(TFT_HEIGHT * 0.40), title_font)
        centered(subtitle, int(TFT_HEIGHT * 0.54), subtitle_font)
    else:
        centered(title, 110, title_font)
        centered(subtitle, 175, subtitle_font)

    return image


def show_tft_status_screen(
    tft: Optional[TFTDevice],
    state: str,
    title: str,
    subtitle: str,
    fault: bool,
) -> None:
    """Paint a status page, suppressing unnecessary repeated redraws."""
    global _tft_last_screen

    if not TFT_ENABLE or not tft or not tft.detected:
        return
    if _tft_last_screen == state:
        return
    if tft.backend != "framebuffer" or not tft.device:
        logging.debug("TFT drawing requires a framebuffer backend; got %s", tft.backend)
        return

    try:
        image = build_status_image(title, subtitle, fault)
    except ImportError:
        logging.error("Pillow is not installed; install python3-pil.")
        return

    if write_image_to_framebuffer(image, tft.device):
        _tft_last_screen = state
        logging.info("TFT status screen updated: %s", title)


def resize_image(image, width: int, height: int, mode: str):
    """Resize a Pillow image using FIT, CROP, or STRETCH semantics."""
    from PIL import Image, ImageOps

    mode = mode.upper().strip()
    source = image.convert("RGB")

    if mode == "CROP":
        return ImageOps.fit(
            source,
            (width, height),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )

    if mode == "STRETCH":
        return source.resize((width, height), Image.Resampling.LANCZOS)

    # FIT is intentionally the default so nobody is cropped out of a selfie.
    contained = ImageOps.contain(source, (width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), IMAGE_BACKGROUND)
    x = (width - contained.width) // 2
    y = (height - contained.height) // 2
    canvas.paste(contained, (x, y))
    return canvas


def apply_station_overlay(image):
    """
    Add a one-line callsign/event caption to the final SSTV image.

    There is no full-width background bar. Instead, the text is drawn directly
    over the image with an outline (stroke) so the background remains visible
    except where the letters themselves appear.
    """
    if not OVERLAY_ENABLE:
        return image

    from PIL import ImageDraw

    output = image.copy().convert("RGB")
    draw = ImageDraw.Draw(output)

    font = load_font(OVERLAY_FONT_SIZE, bold=True)

    callsign = CALLSIGN.strip() or "NOCALL"
    event = EVENT_NAME.strip()

    if event:
        caption = f"{callsign}   {event}"
    else:
        caption = callsign

    box = draw.textbbox(
        (0, 0),
        caption,
        font=font,
        stroke_width=OVERLAY_STROKE_WIDTH,
    )
    text_w = box[2] - box[0]
    text_h = box[3] - box[1]

    x = max(0, (output.width - text_w) // 2)
    y = max(0, output.height - text_h - OVERLAY_PADDING_Y)

    draw.text(
        (x, y),
        caption,
        font=font,
        fill=OVERLAY_TEXT,
        stroke_width=OVERLAY_STROKE_WIDTH,
        stroke_fill=OVERLAY_STROKE_COLOR,
    )

    return output


def build_tft_preview(frame_image):
    """
    Prepare a clean 480x320 live preview for the PiTFT.

    No callsign, event name, camera/device name, or other overlay is drawn on
    the live display. Station identification is added only to the final
    320x240 SSTV image.
    """
    return resize_image(frame_image, TFT_WIDTH, TFT_HEIGHT, "FIT")


def build_tft_transmit_display(sstv_image):
    """
    Prepare the frozen PiTFT display used during SSTV transmission.

    The transmitted 320x240 SSTV image itself is not modified. This helper only
    creates the separate 480x320 on-screen display image and adds a large red
    "TRANSMITTING" banner across the top so visitors know the station is on air.
    """
    from PIL import ImageDraw

    display = resize_image(sstv_image, TFT_WIDTH, TFT_HEIGHT, "FIT")

    if not TRANSMIT_BANNER_ENABLE:
        return display

    draw = ImageDraw.Draw(display)
    font = load_font(TRANSMIT_BANNER_FONT_SIZE, bold=True)

    # Dark strip behind the text to keep the red banner readable regardless of
    # the transmitted picture content.
    banner_h = max(24, int(TRANSMIT_BANNER_BACKGROUND_HEIGHT))
    draw.rectangle(
        [(0, 0), (display.width - 1, banner_h)],
        fill=TRANSMIT_BANNER_BACKGROUND,
    )

    text = TRANSMIT_BANNER_TEXT
    box = draw.textbbox(
        (0, 0),
        text,
        font=font,
        stroke_width=TRANSMIT_BANNER_STROKE_WIDTH,
    )
    text_w = box[2] - box[0]
    text_h = box[3] - box[1]

    x = max(0, (display.width - text_w) // 2)
    y = max(0, (banner_h - text_h) // 2 + TRANSMIT_BANNER_PADDING_Y // 2 - 2)

    draw.text(
        (x, y),
        text,
        font=font,
        fill=TRANSMIT_BANNER_TEXT_COLOR,
        stroke_width=TRANSMIT_BANNER_STROKE_WIDTH,
        stroke_fill=TRANSMIT_BANNER_STROKE_COLOR,
    )

    return display


# ============================================================================
# CAMERA RUNTIME
# ============================================================================

class CameraSource:
    """
    Uniform wrapper around generic V4L2/PyAV and Raspberry Pi Picamera2.

    PyAV is used only as the V4L2 media/camera capture layer. Pillow performs
    the Selfie Station's resize, overlay and JPEG work.
    """

    def __init__(self, camera: CameraDevice):
        self.camera = camera
        self.av_container = None
        self.av_stream = None
        self.av_frames = None
        self.picamera = None

    def open(self) -> None:
        if self.camera.backend == "v4l2":
            try:
                import av
            except ImportError as exc:
                raise RuntimeError(
                    "PyAV is not installed. Install Raspberry Pi OS package python3-av."
                ) from exc

            options = {
                "video_size": f"{CAMERA_CAPTURE_WIDTH}x{CAMERA_CAPTURE_HEIGHT}",
                "framerate": str(CAMERA_FPS),
            }

            if CAMERA_LOW_LATENCY:
                # Keep the V4L2/FFmpeg input queue shallow so the TFT preview
                # favors current frames instead of displaying buffered history.
                options.update({
                    "fflags": "nobuffer",
                    "flags": "low_delay",
                    "thread_queue_size": "1",
                })

            try:
                container = av.open(
                    self.camera.device,
                    mode="r",
                    format="v4l2",
                    options=options,
                )
            except Exception:
                # Some capture devices reject explicit mode requests. Fall back
                # to the device's native/default V4L2 format.
                container = av.open(
                    self.camera.device,
                    mode="r",
                    format="v4l2",
                )

            video_streams = [s for s in container.streams if s.type == "video"]
            if not video_streams:
                container.close()
                raise RuntimeError(f"No video stream found on {self.camera.device}")

            self.av_container = container
            self.av_stream = video_streams[0]

            # For a live preview, extra decode threads can increase buffering.
            # One decode thread gives more predictable latency on small Pis.
            try:
                self.av_stream.thread_type = "NONE"
                self.av_stream.codec_context.thread_count = 1
            except Exception:
                pass

            self.av_frames = container.decode(self.av_stream)
            return

        if self.camera.backend == "libcamera":
            try:
                from picamera2 import Picamera2
            except ImportError as exc:
                raise RuntimeError(
                    "Picamera2 is not installed. Install python3-picamera2."
                ) from exc

            picam = Picamera2(int(self.camera.device))
            config = picam.create_video_configuration(
                main={
                    "size": (CAMERA_CAPTURE_WIDTH, CAMERA_CAPTURE_HEIGHT),
                    "format": "RGB888",
                },
                controls={"FrameRate": CAMERA_FPS},
            )
            picam.configure(config)
            picam.start()
            time.sleep(0.4)
            self.picamera = picam
            return

        raise RuntimeError(f"Unsupported camera backend: {self.camera.backend}")

    def read_rgb(self):
        """Return the next camera frame as an RGB NumPy-compatible array."""
        if self.av_frames is not None:
            try:
                frame = next(self.av_frames)
            except StopIteration:
                return None
            return frame.to_ndarray(format="rgb24")

        if self.picamera is not None:
            return self.picamera.capture_array("main")

        return None

    def close(self) -> None:
        if self.av_container is not None:
            try:
                self.av_container.close()
            except Exception:
                pass
            self.av_container = None
            self.av_stream = None
            self.av_frames = None

        if self.picamera is not None:
            try:
                self.picamera.stop()
                self.picamera.close()
            except Exception:
                pass
            self.picamera = None


def set_latest_frame(frame_rgb) -> None:
    """
    Atomically replace the shared frame with the newest capture.

    There is intentionally no frame queue: older preview frames are discarded
    so the display follows the camera with as little latency as possible.
    """
    global latest_frame_rgb, latest_frame_time
    with frame_lock:
        try:
            latest_frame_rgb = frame_rgb.copy()
        except AttributeError:
            latest_frame_rgb = frame_rgb
        latest_frame_time = time.monotonic()


def get_latest_frame_copy():
    """Return a copy of the latest RGB frame and its monotonic timestamp."""
    with frame_lock:
        if latest_frame_rgb is None:
            return None, latest_frame_time
        try:
            return latest_frame_rgb.copy(), latest_frame_time
        except AttributeError:
            return latest_frame_rgb, latest_frame_time


def camera_worker() -> None:
    """Own the selected camera, capture frames, and drive the live TFT preview."""
    global _tft_last_screen

    opened_camera: Optional[CameraDevice] = None
    source: Optional[CameraSource] = None
    next_preview_time = 0.0
    last_open_failure_time = 0.0

    while not shutdown_event.is_set():
        if station_busy_event.is_set():
            # Once a final SSTV image has been prepared, stop all camera/preview
            # work until transmission completes. Closing the source releases the
            # capture pipeline and prevents decode/poll activity from consuming
            # CPU or adding latency during the transmit phase.
            if source:
                try:
                    source.close()
                except Exception:
                    pass
                source = None
                opened_camera = None
            shutdown_event.wait(0.1)
            continue

        with state_lock:
            desired_camera = active_camera
            tft = active_tft

        if desired_camera != opened_camera:
            if source:
                source.close()
                source = None
            opened_camera = None

            if desired_camera is None:
                shutdown_event.wait(0.2)
                continue

            try:
                source = CameraSource(desired_camera)
                source.open()
                opened_camera = desired_camera
                logging.info(
                    "Camera stream opened: %s (%s)",
                    camera_display_name(desired_camera.name),
                    desired_camera.device,
                )
                _tft_last_screen = None
            except Exception as exc:
                logging.warning("Unable to open camera %s: %s", desired_camera.device, exc)
                source = None
                last_open_failure_time = time.monotonic()
                show_tft_status_screen(
                    tft,
                    state=f"camera-error:{desired_camera.device}",
                    title="CAMERA ERROR",
                    subtitle="Unable to open camera",
                    fault=True,
                )
                shutdown_event.wait(CAMERA_REOPEN_SECONDS)
                continue

        if source is None:
            shutdown_event.wait(0.2)
            continue

        try:
            frame_rgb = source.read_rgb()
        except Exception as exc:
            logging.warning("Camera read failed: %s", exc)
            frame_rgb = None

        if frame_rgb is None:
            if time.monotonic() - last_open_failure_time >= CAMERA_REOPEN_SECONDS:
                logging.warning("Camera stopped returning frames; reopening.")
                last_open_failure_time = time.monotonic()
            source.close()
            source = None
            opened_camera = None
            shutdown_event.wait(CAMERA_REOPEN_SECONDS)
            continue

        set_latest_frame(frame_rgb)

        now = time.monotonic()
        with tft_hold_lock:
            hold_until = tft_hold_until

        if (
            TFT_ENABLE
            and TFT_PREVIEW_ENABLE
            and tft
            and tft.detected
            and tft.backend == "framebuffer"
            and tft.device
            and now >= hold_until
            and now >= next_preview_time
        ):
            try:
                from PIL import Image

                preview = build_tft_preview(
                    Image.fromarray(frame_rgb, mode="RGB")
                )

                # Serialize framebuffer writes with still-image display. A
                # preview frame may have been prepared just before a capture,
                # so re-check the hold after obtaining the display lock.
                with tft_write_lock:
                    with tft_hold_lock:
                        current_hold_until = tft_hold_until

                    if time.monotonic() >= current_hold_until:
                        write_image_to_framebuffer(preview, tft.device)

                next_preview_time = time.monotonic() + (
                    1.0 / max(0.5, TFT_PREVIEW_FPS)
                )
            except Exception as exc:
                logging.debug("TFT preview update failed: %s", exc)

    if source:
        source.close()


# ============================================================================
# ROBOT36 / CHANNEL CLEAR / AIOC TRANSMIT
# ============================================================================

def dbfs_from_pcm16(raw_pcm: bytes) -> tuple[float, float]:
    """
    Return (RMS dBFS, peak dBFS) for little-endian signed 16-bit mono PCM.

    A squelched receiver should produce very low RMS values. The configurable
    RMS threshold is intentionally simple because the attached radio already
    provides the actual squelch decision.
    """
    import numpy as np

    if not raw_pcm:
        return -120.0, -120.0

    samples = np.frombuffer(raw_pcm, dtype="<i2").astype(np.float64)
    if samples.size == 0:
        return -120.0, -120.0

    normalized = samples / 32768.0
    rms = float(np.sqrt(np.mean(normalized * normalized)))
    peak = float(np.max(np.abs(normalized)))

    rms_dbfs = 20.0 * math.log10(max(rms, 1e-6))
    peak_dbfs = 20.0 * math.log10(max(peak, 1e-6))
    return rms_dbfs, peak_dbfs


def record_aioc_rx_pcm(aioc: AIOCDevice, seconds: int) -> Optional[bytes]:
    """
    Record raw mono PCM from the discovered AIOC ALSA capture interface.

    ALSA's arecord is used deliberately so we can address the exact hw:X,0
    interface already associated with the physical AIOC by sysfs discovery.
    """
    if not aioc.alsa_card:
        logging.error("AIOC has no discovered ALSA device; cannot sense channel.")
        return None

    command = [
        "arecord",
        "-q",
        "-D", aioc.alsa_card,
        "-t", "raw",
        "-f", "S16_LE",
        "-r", str(AIOC_AUDIO_RATE),
        "-c", "1",
        "-d", str(max(1, int(seconds))),
        "-",
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(5, int(seconds) + 3),
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logging.error("AIOC receive-audio check failed: %s", exc)
        return None

    if result.returncode != 0:
        logging.error(
            "arecord failed on %s: %s",
            aioc.alsa_card,
            result.stderr.decode(errors="replace").strip() or "unknown error",
        )
        return None

    return result.stdout


def wait_for_clear_channel(aioc: AIOCDevice) -> bool:
    """
    Wait until AIOC receive audio is below CHANNEL_BUSY_RMS_DBFS.

    The radio's squelch provides the carrier/no-carrier decision; we simply
    measure the resulting receive audio level.
    """
    start = time.monotonic()
    consecutive_clear = 0

    logging.info(
        "Checking channel before transmit (busy threshold %.1f dBFS).",
        CHANNEL_BUSY_RMS_DBFS,
    )

    while not shutdown_event.is_set():
        if (
            CHANNEL_CLEAR_TIMEOUT_SECONDS > 0
            and time.monotonic() - start >= CHANNEL_CLEAR_TIMEOUT_SECONDS
        ):
            logging.warning(
                "Channel did not become clear within %.1f seconds; transmit cancelled.",
                CHANNEL_CLEAR_TIMEOUT_SECONDS,
            )
            return False

        raw = record_aioc_rx_pcm(aioc, CHANNEL_CHECK_SECONDS)
        if raw is None:
            logging.warning("Unable to determine channel state; transmit cancelled.")
            return False

        rms_dbfs, peak_dbfs = dbfs_from_pcm16(raw)
        busy = rms_dbfs >= CHANNEL_BUSY_RMS_DBFS

        logging.info(
            "Channel level: RMS %.1f dBFS, peak %.1f dBFS -> %s",
            rms_dbfs,
            peak_dbfs,
            "BUSY" if busy else "CLEAR",
        )

        if busy:
            consecutive_clear = 0
            shutdown_event.wait(CHANNEL_BUSY_RECHECK_SECONDS)
            continue

        consecutive_clear += 1
        if consecutive_clear >= max(1, CHANNEL_CLEAR_CHECKS_REQUIRED):
            logging.info("Channel clear.")
            return True

    return False


def encode_robot36_wav(image_path: Path, wav_path: Path) -> bool:
    """
    Encode the prepared 320x240 JPEG as 48 kHz / 16-bit / mono Robot36 audio.

    PySSTV provides the Robot36 timing/VIS implementation. Samples are written
    in chunks so the generated audio can be level-scaled without keeping the
    entire ~36-second transmission in memory.
    """
    try:
        from PIL import Image
        from pysstv.color import Robot36
    except ImportError as exc:
        logging.error(
            "Robot36 encoder dependency missing: %s. Install PySSTV in the project venv.",
            exc,
        )
        return False

    try:
        image = Image.open(image_path).convert("RGB")
        if image.size != (SSTV_WIDTH, SSTV_HEIGHT):
            logging.error(
                "Robot36 source must be %dx%d; got %dx%d.",
                SSTV_WIDTH,
                SSTV_HEIGHT,
                image.width,
                image.height,
            )
            return False

        wav_path.parent.mkdir(parents=True, exist_ok=True)

        encoder = Robot36(
            image,
            samples_per_sec=SSTV_SAMPLE_RATE,
            bits=SSTV_BITS_PER_SAMPLE,
        )

        gain = max(0.0, min(100.0, float(TX_AUDIO_LEVEL_PERCENT))) / 100.0
        chunk = array("h")
        chunk_samples = 8192

        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SSTV_SAMPLE_RATE)

            for sample in encoder.gen_samples():
                scaled = int(sample * gain)
                if scaled > 32767:
                    scaled = 32767
                elif scaled < -32768:
                    scaled = -32768

                chunk.append(scaled)

                if len(chunk) >= chunk_samples:
                    if sys.byteorder != "little":
                        chunk.byteswap()
                    wav.writeframesraw(chunk.tobytes())
                    chunk = array("h")

            if chunk:
                if sys.byteorder != "little":
                    chunk.byteswap()
                wav.writeframesraw(chunk.tobytes())

        logging.info(
            "Robot36 audio encoded: %s (%d Hz mono, TX level %d%%).",
            wav_path,
            SSTV_SAMPLE_RATE,
            TX_AUDIO_LEVEL_PERCENT,
        )
        return True

    except Exception:
        logging.exception("Robot36 encoding failed.")
        return False


def set_aioc_ptt(aioc: AIOCDevice, asserted: bool):
    """
    Open the AIOC serial interface and set PTT.

    Current AIOC firmware asserts PTT with DTR=1 and RTS=0. Configure both
    modem-control outputs while the pyserial object is still closed, then open
    the port and assert DTR. This minimizes unintended PTT transitions while
    the ttyACM device is being opened.
    """
    if not aioc.tty_device:
        raise RuntimeError("AIOC serial/PTT device was not detected.")

    try:
        import serial
    except ImportError as exc:
        raise RuntimeError(
            "pyserial is not installed. Install python3-serial."
        ) from exc

    port = serial.Serial()
    port.port = aioc.tty_device
    port.baudrate = 9600
    port.timeout = 1
    port.write_timeout = 1

    try:
        port.exclusive = True
    except Exception:
        pass

    # Establish the desired inactive state before opening the device.
    port.rts = False
    port.dtr = False
    port.open()

    # AIOC PTT = DTR high while RTS remains low.
    port.rts = False
    port.dtr = bool(asserted)
    return port


def stream_robot36_to_aioc(
    aioc: AIOCDevice,
    image_path: Path,
    diagnostic_wav_path: Optional[Path] = None,
) -> bool:
    """Generate Robot36 incrementally and stream it directly to the AIOC."""
    if not aioc.alsa_card:
        logging.error("AIOC ALSA device not available.")
        return False

    try:
        from PIL import Image
        from pysstv.color import Robot36
    except ImportError as exc:
        logging.error("Robot36 encoder dependency missing: %s", exc)
        return False

    image = Image.open(image_path).convert("RGB")
    if image.size != (SSTV_WIDTH, SSTV_HEIGHT):
        logging.error(
            "Robot36 source must be %dx%d; got %dx%d.",
            SSTV_WIDTH, SSTV_HEIGHT, image.width, image.height,
        )
        return False

    encoder = Robot36(
        image,
        samples_per_sec=SSTV_SAMPLE_RATE,
        bits=SSTV_BITS_PER_SAMPLE,
    )

    command = [
        "aplay", "-q",
        "-D", aioc.alsa_card,
        "-t", "raw",
        "-f", "S16_LE",
        "-r", str(SSTV_SAMPLE_RATE),
        "-c", "1",
        "-",
    ]

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        logging.error("aplay is not installed (alsa-utils).")
        return False

    wav = None
    try:
        if diagnostic_wav_path is not None:
            diagnostic_wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav = wave.open(str(diagnostic_wav_path), "wb")
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SSTV_SAMPLE_RATE)

        gain = max(0.0, min(100.0, float(TX_AUDIO_LEVEL_PERCENT))) / 100.0
        chunk_samples = 4096  # ~85 ms at 48 kHz
        chunk = array("h")

        for sample in encoder.gen_samples():
            if shutdown_event.is_set():
                break

            scaled = int(sample * gain)
            scaled = max(-32768, min(32767, scaled))
            chunk.append(scaled)

            if len(chunk) >= chunk_samples:
                if sys.byteorder != "little":
                    chunk.byteswap()
                pcm = chunk.tobytes()
                if wav is not None:
                    wav.writeframesraw(pcm)
                if process.stdin is None:
                    raise RuntimeError("aplay stdin unavailable")
                process.stdin.write(pcm)
                chunk = array("h")

        if chunk and not shutdown_event.is_set():
            if sys.byteorder != "little":
                chunk.byteswap()
            pcm = chunk.tobytes()
            if wav is not None:
                wav.writeframesraw(pcm)
            if process.stdin is None:
                raise RuntimeError("aplay stdin unavailable")
            process.stdin.write(pcm)

        if process.stdin is not None:
            process.stdin.close()

        rc = process.wait()
        if rc != 0:
            err = b""
            if process.stderr is not None:
                err = process.stderr.read()
            logging.error(
                "aplay failed on %s: %s",
                aioc.alsa_card,
                err.decode(errors="replace").strip() or "unknown error",
            )
            return False

        return not shutdown_event.is_set()

    except BrokenPipeError:
        logging.error("Robot36 audio pipe closed unexpectedly.")
        return False
    except Exception:
        logging.exception("Robot36 streaming failed.")
        return False
    finally:
        if wav is not None:
            try:
                wav.close()
            except Exception:
                pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


def transmit_robot36(image_path: Path) -> None:
    """
    Transmit one prepared SSTV image.

    Live transmit checks the channel first, keys PTT, then generates Robot36
    samples incrementally while ALSA plays them. This removes the long full-WAV
    pre-render delay from the trigger-to-PTT path.
    """
    if not tx_lock.acquire(blocking=False):
        logging.warning("Robot36 encode/transmit already in progress; request ignored.")
        return

    serial_port = None
    try:
        wav_path = image_path.parent / SSTV_WAV_FILENAME

        if not TRANSMIT_ENABLE:
            logging.info("Transmit disabled; generating diagnostic Robot36 WAV only.")
            encode_robot36_wav(image_path, wav_path)
            return

        with state_lock:
            aioc = active_aioc

        if aioc is None:
            logging.warning("No AIOC detected; image saved but not transmitted.")
            return

        if not aioc.alsa_card or not aioc.tty_device:
            logging.warning(
                "AIOC incomplete (audio=%s, PTT=%s); transmit cancelled.",
                aioc.alsa_card or "missing",
                aioc.tty_device or "missing",
            )
            return

        if not wait_for_clear_channel(aioc):
            return

        logging.info("Asserting AIOC PTT on %s.", aioc.tty_device)
        serial_port = set_aioc_ptt(aioc, True)

        if shutdown_event.wait(PTT_PREKEY_SECONDS):
            return

        logging.info(
            "Streaming Robot36 via %s. Expected on-air time is about 36 seconds.",
            aioc.alsa_card,
        )

        diagnostic = wav_path if SAVE_SSTV_WAV else None
        success = stream_robot36_to_aioc(aioc, image_path, diagnostic)

        if not shutdown_event.is_set():
            shutdown_event.wait(PTT_POSTKEY_SECONDS)

        if success:
            logging.info("Robot36 transmission complete.")
        else:
            logging.error("Robot36 streaming/playback failed.")

    except Exception:
        logging.exception("Robot36 transmit workflow failed.")
    finally:
        if serial_port is not None:
            try:
                serial_port.dtr = False
                serial_port.rts = False
            except Exception:
                pass
            try:
                serial_port.close()
            except Exception:
                pass
            logging.info("AIOC PTT released.")
        tx_lock.release()


# ============================================================================
# CAPTURE / TRIGGER WORKFLOW
# ============================================================================

def save_selfie_from_latest_frame(source_name: str) -> None:
    """Create and save a Robot36-ready still from the latest live camera frame."""
    global tft_hold_until

    if not capture_lock.acquire(blocking=False):
        logging.info("Capture ignored; another capture is already being processed.")
        return

    try:
        frame_rgb, frame_time = get_latest_frame_copy()
        age = time.monotonic() - frame_time if frame_time else 9999.0

        with state_lock:
            camera = active_camera
            tft = active_tft

        if frame_rgb is None or age > CAMERA_FRAME_STALE_SECONDS:
            logging.warning("Trigger received but no current camera frame is available.")
            show_tft_status_screen(
                tft,
                state="capture-no-camera",
                title="NO CAMERA",
                subtitle="Check camera connection",
                fault=True,
            )
            return

        try:
            from PIL import Image
        except ImportError:
            logging.error("Pillow is not installed; cannot save selfie.")
            return

        source_image = Image.fromarray(frame_rgb, mode="RGB")
        sstv_image = resize_image(source_image, SSTV_WIDTH, SSTV_HEIGHT, IMAGE_RESIZE_MODE)
        sstv_image = apply_station_overlay(sstv_image)

        capture_dir = Path(CAPTURE_DIR)
        capture_dir.mkdir(parents=True, exist_ok=True)

        latest_path = capture_dir / LATEST_CAPTURE_FILENAME
        save_args = {"format": "JPEG", "quality": JPEG_QUALITY, "subsampling": 0}

        # latest_sstv.jpg is the working image used by the transmit path and is
        # always overwritten with the newest selfie.
        sstv_image.save(latest_path, **save_args)

        logging.info("Selfie captured from %s", source_name)
        logging.info("Updated latest image: %s", latest_path)

        if SAVE_CAPTURES:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            callsign_for_filename = "".join(
                char if char.isalnum() or char in ("-", "_") else "_"
                for char in (CALLSIGN.strip() or "NOCALL")
            )
            timestamped_path = (
                capture_dir / f"{timestamp}-{callsign_for_filename}.jpg"
            )
            sstv_image.save(timestamped_path, **save_args)
            logging.info("Saved capture: %s", timestamped_path)

        logging.info("Output size: %dx%d", SSTV_WIDTH, SSTV_HEIGHT)

        write_ok = False

        # Show the completed image on the PiTFT. This becomes the "now
        # transmitting" screen and remains frozen there for the entire SSTV TX.
        if tft and tft.detected and tft.backend == "framebuffer" and tft.device:
            display_image = build_tft_transmit_display(sstv_image)

            # Freeze the TFT immediately so no preview frame can overwrite the
            # image we are about to transmit.
            with tft_hold_lock:
                tft_hold_until = float("inf")

            with tft_write_lock:
                write_ok = write_image_to_framebuffer(display_image, tft.device)

            if write_ok:
                logging.info("Transmit image displayed on TFT.")

        # Enter dedicated transmit mode: stop camera preview/polling, stop
        # hardware rescans, ignore additional triggers, and focus exclusively
        # on the SSTV transmit path.
        station_busy_event.set()
        logging.info("Station entering transmit mode: pausing preview and rescans.")

        try:
            transmit_robot36(latest_path)
        finally:
            station_busy_event.clear()
            logging.info("Station leaving transmit mode: preview/rescans resumed.")

            # Keep the transmitted image visible briefly after TX, then allow the
            # live preview to resume normally.
            with tft_hold_lock:
                if write_ok:
                    tft_hold_until = time.monotonic() + CAPTURE_DISPLAY_SECONDS
                else:
                    tft_hold_until = 0.0

    except Exception:
        logging.exception("Selfie capture failed.")
    finally:
        capture_lock.release()


def handle_trigger(source: str) -> None:
    """Common HID/GPIO trigger callback."""
    global last_trigger_time

    if station_busy_event.is_set():
        logging.info("Trigger ignored; station is busy transmitting: %s", source)
        return

    with trigger_lock:
        now = time.monotonic()
        if now - last_trigger_time < TRIGGER_LOCKOUT_SECONDS:
            logging.debug("Trigger ignored during lockout: %s", source)
            return
        last_trigger_time = now

    logging.info("TRIGGER received from %s", source)

    # Keep evdev/gpio callback threads responsive while image processing/saving
    # takes place in a separate worker.
    threading.Thread(
        target=save_selfie_from_latest_frame,
        args=(source,),
        name="selfie-capture",
        daemon=True,
    ).start()


# ============================================================================
# HID / GPIO TRIGGERS
# ============================================================================

def keyboard_listener() -> None:
    """Listen directly to Linux evdev keyboard/HID devices."""
    if not KEYBOARD_TRIGGER_ENABLE:
        logging.info("Keyboard/HID trigger disabled.")
        return

    try:
        from evdev import InputDevice, ecodes, list_devices
    except ImportError:
        logging.error("python3-evdev is not installed; HID trigger disabled.")
        return

    key_code = getattr(ecodes, KEYBOARD_KEY, None)
    if key_code is None:
        logging.error("Unknown KEYBOARD_KEY value: %s", KEYBOARD_KEY)
        return

    known_paths: set[str] = set()

    while not shutdown_event.is_set():
        current_paths = set(list_devices())

        for device_path in current_paths - known_paths:
            try:
                device = InputDevice(device_path)
                capabilities = device.capabilities()
                keys = capabilities.get(ecodes.EV_KEY, [])

                if key_code not in keys:
                    device.close()
                    continue

                if (
                    KEYBOARD_DEVICE_MATCH
                    and KEYBOARD_DEVICE_MATCH.lower() not in device.name.lower()
                ):
                    device.close()
                    continue

                logging.info(
                    "HID trigger device detected: %s (%s)",
                    device.name,
                    device.path,
                )

                threading.Thread(
                    target=read_input_device,
                    args=(device_path, key_code),
                    daemon=True,
                ).start()

            except (OSError, PermissionError) as exc:
                logging.debug("Unable to inspect input device %s: %s", device_path, exc)

        known_paths = current_paths
        shutdown_event.wait(2.0)


def read_input_device(device_path: str, key_code: int) -> None:
    """Read events from one evdev device until it disconnects."""
    try:
        from evdev import InputDevice, ecodes

        device = InputDevice(device_path)
        for event in device.read_loop():
            if shutdown_event.is_set():
                break
            if (
                event.type == ecodes.EV_KEY
                and event.code == key_code
                and event.value == 1
            ):
                handle_trigger(f"HID {device.name} ({device.path})")

    except (OSError, PermissionError) as exc:
        logging.info("HID trigger device disconnected/unavailable: %s", exc)


def gpio_listener() -> None:
    """Configure the GPIO pushbutton using gpiozero."""
    if not GPIO_TRIGGER_ENABLE:
        logging.info("GPIO trigger disabled.")
        return

    try:
        from gpiozero import Button
    except ImportError:
        logging.error("python3-gpiozero is not installed; GPIO trigger disabled.")
        return

    try:
        button = Button(
            GPIO_PIN,
            pull_up=GPIO_PULL_UP,
            bounce_time=GPIO_BOUNCE_TIME,
        )
        button.when_pressed = lambda: handle_trigger(f"GPIO BCM {GPIO_PIN}")
        logging.info("GPIO trigger ready on BCM GPIO %d", GPIO_PIN)

        while not shutdown_event.wait(1.0):
            pass

        button.close()

    except Exception as exc:
        logging.error("Unable to initialize GPIO %d: %s", GPIO_PIN, exc)


# ============================================================================
# HARDWARE MONITOR
# ============================================================================

def hardware_monitor() -> None:
    """Rescan hot-pluggable hardware and publish current camera/TFT state."""
    global active_camera, active_tft, active_aioc, _tft_last_screen

    previous_camera: Optional[CameraDevice] = None
    previous_aioc: Optional[AIOCDevice] = None
    previous_tft: Optional[TFTDevice] = None

    while not shutdown_event.is_set():
        if station_busy_event.is_set():
            shutdown_event.wait(0.25)
            continue

        camera = select_camera(discover_cameras())
        aioc = discover_aioc()
        tft = discover_tft()

        with state_lock:
            active_camera = camera
            active_tft = tft
            active_aioc = aioc

        if camera != previous_camera:
            if camera:
                logging.info(
                    "Selected camera: backend=%s device=%s name=%s",
                    camera.backend,
                    camera.device,
                    camera.name,
                )
            else:
                logging.warning("No camera detected.")
                show_tft_status_screen(
                    tft,
                    state="no-camera",
                    title="NO CAMERA",
                    subtitle="Check camera connection",
                    fault=True,
                )
            previous_camera = camera

        if aioc != previous_aioc:
            if aioc:
                logging.info("AIOC detected: %s", aioc.usb_name)
                logging.info("AIOC USB sysfs path: %s", aioc.usb_path)
                logging.info("AIOC ALSA device: %s", aioc.alsa_card or "not found")
                logging.info("AIOC serial/PTT device: %s", aioc.tty_device or "not found")
            else:
                logging.warning("AIOC not detected.")
            previous_aioc = aioc

        if tft != previous_tft:
            if not tft.enabled:
                logging.info("TFT support disabled in configuration.")
            elif tft.detected:
                logging.info(
                    "PiTFT detected: backend=%s device=%s description=%s",
                    tft.backend,
                    tft.device,
                    tft.description,
                )
                _tft_last_screen = None
                if camera is None:
                    show_tft_status_screen(
                        tft,
                        state="no-camera",
                        title="NO CAMERA",
                        subtitle="Check camera connection",
                        fault=True,
                    )
            else:
                logging.warning("PiTFT enabled but not detected: %s", tft.description)
            previous_tft = tft

        shutdown_event.wait(HARDWARE_RESCAN_SECONDS)


# ============================================================================
# STARTUP / SHUTDOWN
# ============================================================================

def signal_handler(signum, _frame) -> None:
    """Allow systemd/SIGINT to stop worker threads cleanly."""
    logging.info("Shutdown signal received (%s).", signum)
    shutdown_event.set()


def validate_runtime_packages() -> None:
    """Log clear errors for required runtime packages."""
    missing = []

    try:
        import PIL  # noqa: F401
    except ImportError:
        missing.append("python3-pil")

    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("python3-numpy")

    try:
        import serial  # noqa: F401
    except ImportError:
        missing.append("python3-serial")

    try:
        from pysstv.color import Robot36  # noqa: F401
    except ImportError:
        missing.append("PySSTV (pip in project venv)")

    try:
        import av  # noqa: F401
    except ImportError:
        missing.append("python3-av")

    if missing:
        logging.error("Missing required packages: %s", ", ".join(missing))


def main() -> int:
    """Start the SSTV Selfie Station."""
    setup_logging()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logging.info("============================================================")
    logging.info("SSTV Selfie Station")
    logging.info("Callsign: %s", CALLSIGN)
    logging.info("Event: %s", EVENT_NAME)
    logging.info("Robot36 image size: %dx%d", SSTV_WIDTH, SSTV_HEIGHT)
    logging.info("Resize mode: %s", IMAGE_RESIZE_MODE)
    logging.info("PiTFT target: %dx%d rotation %d", TFT_WIDTH, TFT_HEIGHT, TFT_ROTATION)
    logging.info("============================================================")

    validate_runtime_packages()
    map_linux_console_to_hdmi()

    workers = [
        threading.Thread(target=hardware_monitor, name="hardware-monitor", daemon=True),
        threading.Thread(target=camera_worker, name="camera-worker", daemon=True),
        threading.Thread(target=keyboard_listener, name="keyboard-listener", daemon=True),
        threading.Thread(target=gpio_listener, name="gpio-listener", daemon=True),
    ]

    for worker in workers:
        worker.start()

    while not shutdown_event.wait(1.0):
        pass

    logging.info("SSTV Selfie Station stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
