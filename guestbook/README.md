# Wedding Audio Guestbook – Mac Setup

## 1. Install
Put this `guestbook` folder somewhere permanent (e.g. your home folder), then open Terminal:

```bash
cd ~/guestbook
python3 -m venv .venv
source .venv/bin/activate
pip install sounddevice soundfile numpy pygame
```

If `python3` isn't found, macOS will offer to install the Command Line Tools, or install Python from python.org.

## 2. Record your greeting
Record it together with QuickTime or Voice Memos, export as WAV or AIFF, and save it as `greeting.wav` in this folder. (In a pinch, macOS can generate one: `say -o greeting.aiff "Hi! Leave us a message after the beep."` then set `GREETING_FILE` to that name.)

## 3. Plug in hardware and test (in order)
With the venv active (`source .venv/bin/activate`):

1. `python guestbook.py --list-devices` – the handset should appear as **External Microphone** / **External Headphones**, and the encoder under game controllers.
2. `python guestbook.py --mic-test` – macOS will ask for microphone permission the first time; click **Allow**. You should hear yourself in the earpiece.
3. `python guestbook.py --test` – lift and replace the handset. Note which button number changes and set `BUTTON_INDEX` in the script. If UP/DOWN is backwards, flip `PRESSED_MEANS_HUNG_UP`.
4. `python guestbook.py` – full run. Leave a few messages and check `~/Wedding Guestbook Recordings`.

No hardware yet? `python guestbook.py --keyboard` uses the Enter key as the hook switch.

## 4. Optional live backup
Set `SYNC_DIR` in the script to a Google Drive, Dropbox, or iCloud folder. Every message is copied there as it's saved.

## 5. Wedding day
- Plug the Mac into power, set the headphone volume, and double-click `start_guestbook.command` (first time: right-click → Open). It keeps the Mac awake and restarts the guestbook if anything crashes.
- To launch automatically: System Settings → General → Login Items → add `start_guestbook.command`.
- Leave the lid open (closing it sleeps the Mac), screen dimmed, with airflow under the tablecloth.

## Troubleshooting
- **Silent recordings:** System Settings → Privacy & Security → Microphone → enable Terminal.
- **Button never registers in `--test`:** check Privacy & Security → Input Monitoring for Terminal, and try a different USB adapter.
- **Greeting plays from laptop speakers:** the handset plug isn't seated, or the device name differs; check `--list-devices` and update `OUTPUT_DEVICE` / `INPUT_DEVICE`.
- A log of every call is kept in `guestbook.log` in the recordings folder.
