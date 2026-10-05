#!/usr/bin/env python3
"""
Wedding Audio Guestbook (macOS)
===============================
Turns a vintage phone into an audio guestbook:

    lift handset -> greeting plays -> beep -> guest talks -> hang up to save

Hardware:
  * Handset earpiece + mic wired to a 4-pole (TRRS) headset plug,
    plugged into the MacBook's headphone jack
  * Hook switch wired to one input of a "zero delay" USB arcade encoder,
    which shows up on the Mac as a game controller

Run modes:
  python guestbook.py                 normal operation
  python guestbook.py --test          live view of the hook switch / controller buttons
  python guestbook.py --list-devices  list audio devices and game controllers
  python guestbook.py --mic-test      record 5 s from the handset, then play it back
  python guestbook.py --keyboard      use the Enter key as the hook switch (no hardware needed)
"""

import argparse
import datetime
import logging
import os
import queue
import shutil
import sys
import threading
import time

# Must be set before pygame is imported so the controller keeps working
# even when the Terminal window isn't focused.
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np
import sounddevice as sd
import soundfile as sf

# =============================================================================
# SETTINGS - adjust these to match your setup
# =============================================================================

HERE = os.path.dirname(os.path.abspath(__file__))

# Your recorded greeting (WAV or AIFF). If missing, guests just hear the beep.
GREETING_FILE = os.path.join(HERE, "greeting.wav")

# Where recordings are saved on the Mac.
RECORDING_DIR = os.path.expanduser("~/Wedding Guestbook Recordings")

# Optional second copy in a cloud-synced folder (live backup during the party).
# Examples:
#   Google Drive: "~/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive/Guestbook"
#   Dropbox:      "~/Dropbox/Guestbook"
#   iCloud:       "~/Library/Mobile Documents/com~apple~CloudDocs/Guestbook"
# Set to None to disable.
SYNC_DIR = None

# Audio devices, matched by part of their name (see --list-devices).
# When a headset plug is inserted, macOS usually names these as below.
# Set to None to use the Mac's current default device.
INPUT_DEVICE = "External Microphone"
OUTPUT_DEVICE = "External Headphones"

# Which controller button the hook switch is wired to (find it with --test).
BUTTON_INDEX = 0

# True if the button reads as "pressed" while the handset rests in the cradle.
# If --test shows UP/DOWN backwards, flip this.
PRESSED_MEANS_HUNG_UP = True

MAX_RECORD_SECONDS = 180    # recording stops automatically after this
MIN_KEEP_SECONDS = 2.0      # shorter recordings (accidental bumps) are discarded
PICKUP_DELAY = 0.6          # pause after lifting so the guest can raise it to their ear
DEBOUNCE_SECONDS = 0.15     # switch must be steady this long before a change counts
POLL_INTERVAL = 0.01

BEEP_FREQ = 1000
BEEP_SECONDS = 0.45
BEEP_VOLUME = 0.25

# =============================================================================

log = logging.getLogger("guestbook")


def setup_logging():
    os.makedirs(RECORDING_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    logfile = logging.FileHandler(os.path.join(RECORDING_DIR, "guestbook.log"))
    logfile.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    log.addHandler(logfile)


# -----------------------------------------------------------------------------
# Hook switch sources
# -----------------------------------------------------------------------------

class JoystickHook:
    """Reads the hook switch from a USB arcade encoder (game controller)."""

    def __init__(self):
        import pygame
        self.pg = pygame
        pygame.init()
        pygame.joystick.init()
        self.js = None
        self._last_warning = 0.0
        self._connect()

    def _connect(self):
        if self.pg.joystick.get_count() > 0:
            self.js = self.pg.joystick.Joystick(0)
            self.js.init()
            log.info(f"Controller connected: {self.js.get_name()} "
                     f"({self.js.get_numbuttons()} buttons)")

    def _pump(self):
        for event in self.pg.event.get():
            if event.type == self.pg.JOYDEVICEREMOVED:
                log.warning("Controller disconnected!")
                self.js = None
            elif event.type == self.pg.JOYDEVICEADDED and self.js is None:
                self._connect()

    def pressed_buttons(self):
        self._pump()
        if self.js is None:
            return None
        return [i for i in range(self.js.get_numbuttons()) if self.js.get_button(i)]

    def raw_off_hook(self):
        """True = handset lifted, False = resting, None = no controller."""
        self._pump()
        if self.js is None or BUTTON_INDEX >= self.js.get_numbuttons():
            now = time.monotonic()
            if now - self._last_warning > 10:
                log.warning("No controller found - treating handset as hung up.")
                self._last_warning = now
            return None
        pressed = bool(self.js.get_button(BUTTON_INDEX))
        hung_up = pressed if PRESSED_MEANS_HUNG_UP else not pressed
        return not hung_up


class KeyboardHook:
    """Press Enter to lift / hang up. For testing without hardware."""

    def __init__(self):
        self.off_hook = False
        threading.Thread(target=self._reader, daemon=True).start()
        print("Keyboard mode: press Enter to lift the handset, Enter again to hang up.")

    def _reader(self):
        while True:
            try:
                input()
            except EOFError:
                return
            self.off_hook = not self.off_hook
            print(">>> handset", "LIFTED" if self.off_hook else "HUNG UP")

    def pressed_buttons(self):
        return None

    def raw_off_hook(self):
        return self.off_hook


class DebouncedHook:
    """Filters out the flicker that mechanical switches make when they move."""

    def __init__(self, source):
        self.source = source
        self.state = False          # False = on hook
        self.candidate = None
        self.since = 0.0

    def read(self):
        raw = self.source.raw_off_hook()
        if raw is None:
            raw = False
        now = time.monotonic()
        if raw != self.state:
            if self.candidate != raw:
                self.candidate, self.since = raw, now
            elif now - self.since >= DEBOUNCE_SECONDS:
                self.state, self.candidate = raw, None
        else:
            self.candidate = None
        return self.state


# -----------------------------------------------------------------------------
# Audio helpers
# -----------------------------------------------------------------------------

def resolve_device(name_part, kind):
    if not name_part:
        return None
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, dev in enumerate(sd.query_devices()):
        if name_part.lower() in dev["name"].lower() and dev[key] > 0:
            return i
    log.warning(f"!! {kind} device '{name_part}' not found - using the Mac's default. "
                f"Is the handset plugged in?")
    return None


def output_format(device):
    info = sd.query_devices(device, "output")
    return int(info["default_samplerate"]), max(1, min(2, info["max_output_channels"]))


def to_channels(data, channels):
    """Play mono audio on both ears, so it works whichever earbud you wired in."""
    if data.ndim == 1:
        data = data[:, None]
    if data.shape[1] < channels:
        data = np.repeat(data[:, :1], channels, axis=1)
    return np.ascontiguousarray(data[:, :channels], dtype=np.float32)


def make_beep(sr, channels, count=1):
    n = int(sr * BEEP_SECONDS)
    t = np.arange(n) / sr
    tone = np.sin(2 * np.pi * BEEP_FREQ * t) * BEEP_VOLUME
    fade = int(sr * 0.01)
    env = np.ones(n)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    tone *= env
    gap = np.zeros(int(sr * 0.15))
    parts = []
    for i in range(count):
        parts.append(tone)
        if i < count - 1:
            parts.append(gap)
    return to_channels(np.concatenate(parts).astype(np.float32), channels)


def load_greeting(channels):
    if not os.path.exists(GREETING_FILE):
        return None, None
    data, sr = sf.read(GREETING_FILE, dtype="float32", always_2d=True)
    return to_channels(data, channels), sr


def play_interruptible(data, sr, hook, device):
    """Play audio; stop early if the guest hangs up. Returns True if finished."""
    sd.play(data, sr, device=device)
    deadline = time.monotonic() + len(data) / sr + 2.0
    try:
        while time.monotonic() < deadline:
            if not hook.read():
                return False
            if not sd.get_stream().active:
                return True
            time.sleep(POLL_INTERVAL)
        return True
    finally:
        sd.stop()


def sleep_interruptible(seconds, hook):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not hook.read():
            return False
        time.sleep(POLL_INTERVAL)
    return True


def record(hook, device):
    """Record until hang-up or time limit. Returns (path, seconds, reason) or None."""
    sr = int(sd.query_devices(device, "input")["default_samplerate"])
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    final_path = os.path.join(RECORDING_DIR, f"guestbook_{stamp}.wav")
    temp_path = final_path + ".part"

    chunks = queue.Queue()
    overflows = [0]

    def callback(indata, frames, time_info, status):
        if status:
            overflows[0] += 1
        chunks.put(indata.copy())

    out = sf.SoundFile(temp_path, "w", samplerate=sr, channels=1,
                       subtype="PCM_16", format="WAV")
    stream = sd.InputStream(device=device, channels=1, samplerate=sr,
                            dtype="float32", callback=callback)
    log.info("Recording...")
    start = time.monotonic()
    reason = "hung up"
    stream.start()
    try:
        while True:
            while not chunks.empty():
                out.write(chunks.get_nowait())
            if not hook.read():
                break
            if time.monotonic() - start >= MAX_RECORD_SECONDS:
                reason = "time limit"
                break
            time.sleep(POLL_INTERVAL)
    finally:
        stream.stop()
        stream.close()
        while not chunks.empty():
            out.write(chunks.get_nowait())
        seconds = out.frames / sr
        out.close()

    if overflows[0]:
        log.info(f"(note: {overflows[0]} audio buffer hiccups)")

    if seconds < MIN_KEEP_SECONDS:
        os.remove(temp_path)
        log.info(f"Discarded {seconds:.1f}s recording (too short).")
        return None

    os.rename(temp_path, final_path)
    log.info(f"Saved {os.path.basename(final_path)}  ({seconds:.0f}s, {reason})")

    if SYNC_DIR:
        try:
            sync = os.path.expanduser(SYNC_DIR)
            os.makedirs(sync, exist_ok=True)
            shutil.copy2(final_path, sync)
        except Exception as e:
            log.warning(f"Could not copy to sync folder: {e}")

    return final_path, seconds, reason


# -----------------------------------------------------------------------------
# Main flow
# -----------------------------------------------------------------------------

def wait_for(hook, off_hook):
    while hook.read() != off_hook:
        time.sleep(POLL_INTERVAL)


def one_guest(hook):
    """Handle a single pickup. Returns 1 if a message was saved."""
    out_dev = resolve_device(OUTPUT_DEVICE, "output")
    in_dev = resolve_device(INPUT_DEVICE, "input")
    out_sr, out_ch = output_format(out_dev)

    if not sleep_interruptible(PICKUP_DELAY, hook):
        return 0

    greeting, g_sr = load_greeting(out_ch)
    if greeting is not None:
        if not play_interruptible(greeting, g_sr, hook, out_dev):
            log.info("Hung up during greeting.")
            return 0

    if not play_interruptible(make_beep(out_sr, out_ch), out_sr, hook, out_dev):
        return 0

    result = record(hook, in_dev)
    if result is None:
        return 0
    if result[2] == "time limit":
        play_interruptible(make_beep(out_sr, out_ch, count=2), out_sr, hook, out_dev)
    return 1


def run(source):
    hook = DebouncedHook(source)
    if not os.path.exists(GREETING_FILE):
        log.warning(f"No greeting found at {GREETING_FILE} - guests will only hear a beep.")
    log.info(f"Saving recordings to {RECORDING_DIR}")

    # Let the switch reading settle, then make sure we start with the handset down.
    settle_end = time.monotonic() + 0.5
    while time.monotonic() < settle_end:
        hook.read()
        time.sleep(POLL_INTERVAL)
    if hook.read():
        log.info("Handset is off the hook - waiting for it to be hung up...")
    wait_for(hook, off_hook=False)

    saved = 0
    log.info("Ready! Waiting for a guest.")
    while True:
        wait_for(hook, off_hook=True)
        log.info("Handset lifted.")
        try:
            saved += one_guest(hook)
        except Exception:
            log.exception("Something went wrong during this call - recovering.")
            time.sleep(1)
        wait_for(hook, off_hook=False)
        log.info(f"Handset down. Messages saved this session: {saved}. Ready.")


def test_mode(source):
    hook = DebouncedHook(source)
    print("Lift and replace the handset. Ctrl+C to quit.\n")
    last = None
    while True:
        state = "UP (lifted)" if hook.read() else "DOWN (hung up)"
        buttons = source.pressed_buttons()
        line = f"Handset: {state}"
        if buttons is not None:
            line += f"   buttons pressed: {buttons}"
        elif isinstance(source, JoystickHook):
            line += "   (no controller detected)"
        if line != last:
            print(line)
            last = line
        time.sleep(0.02)


def mic_test():
    in_dev = resolve_device(INPUT_DEVICE, "input")
    out_dev = resolve_device(OUTPUT_DEVICE, "output")
    sr = int(sd.query_devices(in_dev, "input")["default_samplerate"])
    print("Recording 5 seconds - talk into the handset now...")
    audio = sd.rec(int(5 * sr), samplerate=sr, channels=1, device=in_dev, dtype="float32")
    sd.wait()
    peak = float(np.abs(audio).max())
    print(f"Peak level: {peak:.3f}")
    if peak < 0.002:
        print("That's nearly silent. Check that Terminal has microphone permission "
              "(System Settings > Privacy & Security > Microphone) and the mic wiring.")
    _, out_ch = output_format(out_dev)
    print("Playing it back through the earpiece...")
    sd.play(to_channels(audio[:, 0], out_ch), sr, device=out_dev)
    sd.wait()


def list_devices():
    print("AUDIO DEVICES")
    print(sd.query_devices())
    print("\nGAME CONTROLLERS")
    import pygame
    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        print("  none found")
    for i in range(pygame.joystick.get_count()):
        js = pygame.joystick.Joystick(i)
        js.init()
        print(f"  [{i}] {js.get_name()} - {js.get_numbuttons()} buttons")


def main():
    parser = argparse.ArgumentParser(description="Wedding audio guestbook")
    parser.add_argument("--test", action="store_true", help="show hook switch state live")
    parser.add_argument("--list-devices", action="store_true", help="list audio devices and controllers")
    parser.add_argument("--mic-test", action="store_true", help="record 5 s and play it back")
    parser.add_argument("--keyboard", action="store_true", help="use Enter key as the hook switch")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return
    if args.mic_test:
        mic_test()
        return

    setup_logging()
    source = KeyboardHook() if args.keyboard else JoystickHook()
    try:
        if args.test:
            test_mode(source)
        else:
            run(source)
    except KeyboardInterrupt:
        log.info("Guestbook stopped.")


if __name__ == "__main__":
    main()
