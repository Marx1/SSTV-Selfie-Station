# SSTV Selfie Station

**SSTV Selfie Station** is a Raspberry Pi-based photo booth that captures a selfie and transmits it over amateur radio using Robot36 SSTV.

The station is designed to run unattended at events. A visitor presses a button or USB footswitch, the current camera image is captured, cropped to 320x240, labeled with the configured callsign and event name, checked against receive audio for a clear channel, and transmitted through an AIOC-connected radio.

The PiTFT provides a live framing preview. Once a photo is taken, the display freezes on the image being sent and shows **TRANSMITTING** across the top until the transmission is complete.

## AI NOTICE

This project was 100% vibe coded. I did this as a quick-down and dirty job to fill a need for Pacificon 2026. Use it as insparation for other projects, or as is. 

## Key Features

- Robot36 SSTV transmission from a live camera image.
- 320x240 SSTV output with configurable callsign and event text.
- USB/V4L2 camera and Raspberry Pi CSI camera support.
- Automatic AIOC USB, ALSA, and serial/PTT discovery.
- Receive-audio clear-channel detection using the radio's squelch.
- Serial PTT through the AIOC using DTR/RTS.
- Low-latency Robot36 streaming directly to ALSA.
- Adafruit 3.5" PiTFT live preview.
- HID keyboard/footswitch and GPIO trigger support.
- Automatic camera reconnect.
- Optional timestamped capture archive.
- Automatic startup with systemd.
- Does not require console auto-login.

## Hardware

The software is intended for Raspberry Pi 3B through Raspberry Pi 5.

Typical hardware:

- Raspberry Pi.
- Adafruit 3.5" resistive PiTFT, Product ID 2441.
- USB camera, HDMI capture device, or Raspberry Pi CSI camera.
- AIOC All-In-One-Cable.
- Amateur radio with working squelch.
- USB HID trigger or GPIO pushbutton.

The default HID trigger is a keyboard-style footswitch that sends lowercase `b`.

## Installation

Start with a fresh Raspberry Pi OS installation.

Install the required packages:

```bash
sudo apt update
sudo apt install -y \
    git \
    python3 \
    python3-pip \
    python3-venv \
    python3-gpiozero \
    python3-lgpio \
    python3-evdev \
    python3-pil \
    python3-numpy \
    python3-av \
    python3-serial \
    v4l-utils \
    alsa-utils \
    fbset
```

For a Raspberry Pi CSI camera:

```bash
sudo apt install -y python3-picamera2
```

Create the project directory:

```bash
sudo mkdir -p /opt/sstv-selfie-station
sudo chown "$USER":"$USER" /opt/sstv-selfie-station
```

Copy the project files into:

```text
/opt/sstv-selfie-station/
```

Create the Python environment:

```bash
cd /opt/sstv-selfie-station
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install PySSTV
```

## PiTFT Setup

The project is designed around the Adafruit 3.5" resistive PiTFT, Product ID 2441.

Using Adafruit's Raspberry Pi installer scripts:

```bash
cd ~
python3 -m venv env --system-site-packages
source env/bin/activate
pip3 install --upgrade adafruit-python-shell click Flask-SQLAlchemy
git clone https://github.com/adafruit/Raspberry-Pi-Installer-Scripts.git
cd Raspberry-Pi-Installer-Scripts
sudo -E env PATH=$PATH python3 adafruit-pitft.py \
    --display=35r \
    --rotation=270 \
    --install-type=console
sudo reboot
```

The software detects the PiTFT by framebuffer driver name rather than assuming it will always be `/dev/fb0` or `/dev/fb1`.

If HDMI is connected, Linux console framebuffers are automatically mapped to the HDMI framebuffer where possible.

### Disable the tty1 Cursor

Install the included cursor service:

```bash
cd /opt/sstv-selfie-station
sudo cp hide-tty1-cursor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hide-tty1-cursor.service
```

This prevents the Linux virtual-terminal cursor from blinking over the PiTFT image.

## Configuration

Configuration values are near the top of `sstv_selfie_station.py`.

### Station Identification

- `CALLSIGN`: Callsign placed on the transmitted SSTV image. Default is `NOCALL`.
- `EVENT_NAME`: Event or station name placed beside the callsign.
- `OVERLAY_ENABLE`: Enables or disables the SSTV image text overlay.
- `OVERLAY_FONT_SIZE`: Font size used on the transmitted image.

The live camera preview does not include this text. It is added only to the final SSTV image.

### Camera

- `PREFERRED_CAMERA_NAME`: Optional camera-name match. Leave blank for automatic selection.
- `CAMERA_CAPTURE_WIDTH`: Requested camera width. Default is `1280`.
- `CAMERA_CAPTURE_HEIGHT`: Requested camera height. Default is `720`.
- `CAMERA_FPS`: Camera frame rate. Default is `10`.
- `CAMERA_LOW_LATENCY`: Reduces frame buffering for a more responsive preview.
- `IMAGE_RESIZE_MODE`: `CROP`, `FIT`, or `STRETCH`. Default is `CROP`.

Internal Raspberry Pi ISP capture nodes are excluded automatically.

### PiTFT

- `TFT_ENABLE`: Enables PiTFT output.
- `TFT_PREVIEW_ENABLE`: Enables live camera preview.
- `TFT_PREVIEW_FPS`: PiTFT preview refresh rate. Default is `10`.
- `CAPTURE_DISPLAY_SECONDS`: Time the transmitted image remains on screen after transmission before live preview resumes.
- `TRANSMIT_BANNER_ENABLE`: Enables the on-screen `TRANSMITTING` indication.
- `TRANSMIT_BANNER_TEXT`: Text displayed while the station is transmitting.

The `TRANSMITTING` indication is only shown on the PiTFT. It is not included in the SSTV image sent over the radio.

### Trigger

- `KEYBOARD_TRIGGER_ENABLE`: Enables HID keyboard-style triggering.
- `KEYBOARD_KEY`: Linux input key used for the trigger. Default is `KEY_B`.
- `KEYBOARD_DEVICE_MATCH`: Optional device-name restriction.
- `GPIO_TRIGGER_ENABLE`: Enables GPIO triggering.
- `GPIO_PIN`: BCM GPIO number. Default is `17`.
- `GPIO_PULL_UP`: Enables the GPIO internal pull-up.
- `TRIGGER_LOCKOUT_SECONDS`: Prevents duplicate triggers.

### Captures

- `SAVE_CAPTURES`: When `True`, saves a timestamped JPEG for every selfie.
- `CAPTURE_DIR`: Directory used for captures and the working SSTV image.
- `LATEST_CAPTURE_FILENAME`: Working image used by the transmit path.
- `JPEG_QUALITY`: JPEG quality used when saving images.

`latest_sstv.jpg` is always maintained because it is the working transmit image.

With:

```python
SAVE_CAPTURES = True
```

the station also saves files such as:

```text
captures/20261005-214625-NOCALL.jpg
```

Set it to `False` if you do not want to retain a history of visitor images.

### AIOC

- `AIOC_USB_VID`: AIOC USB vendor ID.
- `AIOC_USB_PID`: AIOC USB product ID.
- `AIOC_NAME_MATCHES`: USB-name matches used during discovery.
- `AIOC_AUDIO_RATE`: Audio sample rate. Default is `48000`.

The AIOC's ALSA card number and `/dev/ttyACM*` device are discovered dynamically. Fixed card numbers or serial-device names are not required.

### SSTV Transmission

- `TRANSMIT_ENABLE`: Enables radio transmission.
- `SSTV_SAMPLE_RATE`: Robot36 sample rate. Default is `48000`.
- `SSTV_BITS_PER_SAMPLE`: Audio sample size. Default is `16`.
- `TX_AUDIO_LEVEL_PERCENT`: SSTV transmit audio level.
- `PTT_PREKEY_SECONDS`: Delay between PTT assertion and SSTV audio.
- `PTT_POSTKEY_SECONDS`: Delay between the end of SSTV audio and PTT release.
- `SAVE_SSTV_WAV`: Optionally saves the transmitted Robot36 audio to a WAV file.

Robot36 audio is generated and streamed during transmission instead of rendering the complete WAV before keying the radio.

## Clear-Channel Detection

Before each transmission, the station records receive audio through the AIOC.

The radio's own squelch is used as the carrier detector. If receive audio is present above the configured level, the channel is treated as busy and the station waits before trying again.

Settings:

- `CHANNEL_BUSY_RMS_DBFS`: Receive-audio threshold used to determine BUSY/CLEAR.
- `CHANNEL_CHECK_SECONDS`: Length of each receive-audio sample.
- `CHANNEL_CLEAR_CHECKS_REQUIRED`: Number of clear samples required before transmitting.
- `CHANNEL_BUSY_RECHECK_SECONDS`: Delay between checks while busy.
- `CHANNEL_CLEAR_TIMEOUT_SECONDS`: Maximum time to wait. Set to `0` to wait indefinitely.

A typical log looks like:

```text
Channel level: RMS -49.3 dBFS, peak -46.2 dBFS -> CLEAR
Channel clear.
Asserting AIOC PTT on /dev/ttyACM0.
Streaming Robot36 via hw:1,0.
```

If the squelch opens because of noise, the received audio is treated as channel activity and transmission is delayed until the receiver becomes quiet again.

## Using the Station

Normal operation is automatic.

The sequence is:

1. The PiTFT displays the live camera preview.
2. A visitor presses the HID or GPIO trigger.
3. The current camera frame is captured.
4. The image is center-cropped to 320x240.
5. Callsign and event text are added.
6. The PiTFT freezes on the completed image.
7. Camera polling and display updates stop.
8. The station checks receive audio for a clear channel.
9. Once clear, the AIOC keys the radio.
10. Robot36 audio is streamed through the AIOC.
11. PTT is released.
12. Live preview resumes.

While this sequence is active, additional trigger presses are ignored.

## Automatic Startup

Install the included service:

```bash
cd /opt/sstv-selfie-station
sudo cp sstv-selfie-station.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sstv-selfie-station.service
```

Check status:

```bash
systemctl status sstv-selfie-station.service
```

Follow the log:

```bash
sudo journalctl -u sstv-selfie-station.service -f
```

The service runs independently of a desktop or console login. Raspberry Pi OS console auto-login can remain disabled.

## Files

```text
sstv_selfie_station.py
sstv-selfie-station.service
hide-tty1-cursor.service
requirements.txt
README.md
captures/
```

When capture history is enabled, timestamped JPEGs are stored under `captures/`.

The current transmit image is:

```text
captures/latest_sstv.jpg
```

If `SAVE_SSTV_WAV = True`, the current transmitted audio is also written as:

```text
captures/latest_sstv.wav
```

## Notes

- Robot36 uses a 320x240 image.
- The PiTFT display is 480x320 and is used only as a local preview/status display.
- The red `TRANSMITTING` text shown on the PiTFT is not part of the transmitted image.
- AIOC audio is 48 kHz mono.
- PTT is asserted using DTR high with RTS low.
- The station pauses camera processing and hardware rescans during transmission to reduce CPU load and transmission latency.
- With noisy radios, adjust the squelch first and then tune `CHANNEL_BUSY_RMS_DBFS` as needed.
