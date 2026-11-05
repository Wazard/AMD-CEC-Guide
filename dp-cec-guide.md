# TV gaming PC setup: HDMI-CEC on/off, auto sign-in, Playnite fullscreen

A Windows PC connected to the TV through a DisplayPort→HDMI adapter:

- **Part 1:** turns the TV on at logon/wake and puts it in standby at sleep/shutdown (HDMI-CEC).
- **Part 2:** signs in automatically and opens Playnite in fullscreen mode.

> **Status:** TV on at wake and off at sleep both work. Shutdown is not tested yet. Part 2 is not tested yet.

# Part 1: HDMI-CEC TV on/off

---

## Setup

| Item | Value |
|---|---|
| GPU | AMD Radeon RX 6700 XT |
| OS | Windows |
| Connection | PC DisplayPort → UGREEN DP→HDMI adapter → TV |
| Goal | TV on at logon/wake, TV standby at sleep/shutdown |

---

## Background: why this works

- AMD's Windows driver does not support HDMI-CEC, and DisplayPort has no CEC wire.
- The UGREEN adapter supports **DisplayPort 1.3 "CEC tunneling over AUX"**. CEC worked through it on Bazzite (Linux), whose `amdgpu` driver supports this. On Windows, `probe` reports capability `0x07`: tunneling, snooping and multiple logical addresses.
- The script drives the adapter's CEC registers (**DPCD 0x3000–0x30FF**) directly over the DP AUX channel.
- It uses the open-source Python library **gpu_aux** (<https://github.com/cnbright/gpu_aux>, `pip install gpu-aux`), which calls AMD's private `ADL_Display_NativeAUXChannel_Access` in `atiadlxx.dll`.

### What the script does

1. Reads the tunneling capability register (`0x3000`).
2. Enables CEC tunneling (`0x3001`).
3. Writes the CEC frame to the TX buffer (`0x3020`), triggers transmission via `0x3003`, and polls the IRQ flags (`0x3004`).

| Command | CEC message | Frame |
|---|---|---|
| `on` | Image View On (to TV) | `40 04` |
| `standby` | Standby (to TV) | `40 36` |

It sends as Playback Device 1 (logical address 4).

### Quirks found on this setup

- **Multi-byte AUX writes fail** with `ADL error -1`, but 1-byte writes work. The script writes every register one byte at a time (`wr()` helper).
- **The logical address mask (`0x300E`/`0x300F`) can't be written** (`ADL error -1`). The script skips it. The mask is only needed to receive messages *from* the TV, not to send on/standby.
- **Video can drop after `standby`.** Run manually, the TV showed "no signal" instead of turning off. Unplugging and replugging the adapter brought the picture back.
- **Windows "entering sleep" task triggers fire too late.** A task triggered by Kernel-Power event 42 only ran *after* the PC woke up. That turned the TV off at wake, and the `on` sent right after was ignored while the TV was shutting down. That's why sleep is handled by the `watch` listener instead.
- The ADL AUX interface is private and undocumented. An Adrenalin driver update could break it.

---

## 1. Install

```bat
winget install Python.Python.3.12
```

Close and reopen the terminal, then:

```bat
pip install gpu-aux
```

---

## 2. Script: `C:\cec\dpcec.py`

Create the folder `C:\cec` and save this as `C:\cec\dpcec.py`.

Commands: `list` | `probe` | `on` | `standby` | `watch` (options: `--gpu N`, `--port N`)

```python
# dpcec.py - HDMI-CEC TV on/off through a DP->HDMI adapter (AMD GPU, Windows)
# Commands: list | probe | on | standby | watch   (add --port N)
import argparse, ctypes, sys, time
from gpu_aux import AuxPort, AuxError, enumerate_gpus, enumerate_ports

if sys.stdout is None:  # pythonw (scheduled tasks): log to file, line-buffered
    sys.stdout = sys.stderr = open(r"C:\cec\dpcec.log", "a", buffering=1)

CAP, CTL, TX_INFO, IRQ, TX_BUF = 0x3000, 0x3001, 0x3003, 0x3004, 0x3020
TX_SENT, LINE_ERR, ADDR_NACK, DATA_NACK, TX_ALL = 0x10, 0x20, 0x40, 0x80, 0xF0
LA = 4  # Playback Device 1
PBT_APMSUSPEND, DEVICE_NOTIFY_CALLBACK = 0x4, 2

def log(*msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *msg)

def wr(p, addr, data):
    # AMD ADL rejects multi-byte native AUX writes (ADL -1), so write one byte at a time
    for i, b in enumerate(data):
        try:
            p.write_dpcd(addr + i, bytes([b]))
        except AuxError as e:
            raise AuxError(f"write 0x{addr + i:04X} = {b:02X}: {e}")

def list_ports():
    for gi, gpu in enumerate(enumerate_gpus("AMD")):
        print(f"GPU {gi}: {gpu.name}")
        counts = {}
        for port in enumerate_ports("AMD", gi):
            idx = counts.get(port.kind, 0); counts[port.kind] = idx + 1
            print(f"  {port.kind} --port {idx}   {port.name}")

def cec_enable(p):
    cap = p.read_dpcd(CAP, 1)[0]
    print(f"CEC tunneling capability: 0x{cap:02x}")
    if not cap & 0x01:
        raise AuxError("this adapter/port does not report CEC tunneling")
    wr(p, CTL, b"\x01")
    # No LA mask (0x300E): ADL refuses that write, and it's only needed to receive

def cec_send(p, msg):
    wr(p, IRQ, bytes([TX_ALL]))
    wr(p, TX_BUF, bytes(msg))
    wr(p, TX_INFO, bytes([(len(msg) - 1) | (2 << 4) | 0x80]))
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        time.sleep(0.01)
        irq = p.read_dpcd(IRQ, 1)[0] & TX_ALL
        if irq:
            wr(p, IRQ, bytes([irq]))
            if irq & TX_SENT:   print("OK: TV acknowledged"); return True
            if irq & ADDR_NACK: print("FAIL: TV did not answer (CEC off on TV?)")
            if irq & DATA_NACK: print("FAIL: TV rejected the message")
            if irq & LINE_ERR:  print("FAIL: CEC line error")
            return False
    print("FAIL: timeout")
    return False

def run(cmd, a):
    with AuxPort("DP", index=a.port, gpu_index=a.gpu, backend="AMD") as p:
        cec_enable(p)
        if cmd == "on":
            return cec_send(p, [(LA << 4) | 0x0, 0x04])   # Image View On
        if cmd == "standby":
            return cec_send(p, [(LA << 4) | 0x0, 0x36])   # Standby
        return True                                        # probe

def watch(a):
    # Windows calls on_power before the PC sleeps (and waits ~2 s for it), unlike the
    # Kernel-Power 42 task trigger, which only got to run after wake.
    CB = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p)
    class Params(ctypes.Structure):
        _fields_ = [("callback", CB), ("context", ctypes.c_void_p)]
    def on_power(ctx, kind, setting):
        if kind == PBT_APMSUSPEND:
            log("sleep -> standby")
            try:
                run("standby", a)
            except Exception as e:  # never let one failure kill the watcher
                print(f"standby failed: {e}")
        return 0
    cb = CB(on_power)  # keep a reference so it isn't garbage-collected
    params, handle = Params(cb, None), ctypes.c_void_p()
    rc = ctypes.WinDLL("powrprof").PowerRegisterSuspendResumeNotification(
        DEVICE_NOTIFY_CALLBACK, ctypes.byref(params), ctypes.byref(handle))
    if rc:
        sys.exit(f"PowerRegisterSuspendResumeNotification failed: {rc}")
    log("watching for sleep")
    while True:
        time.sleep(3600)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "probe", "on", "standby", "watch"])
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    if a.cmd == "list":
        return list_ports()
    if a.cmd == "watch":
        return watch(a)
    log(a.cmd)
    sys.exit(0 if run(a.cmd, a) else 1)

if __name__ == "__main__":
    try:
        main()
    except AuxError as e:
        sys.exit(f"AUX error: {e}")
```

---

## 3. Find the port and check the adapter

```bat
python C:\cec\dpcec.py list
python C:\cec\dpcec.py probe --port 0
```

`list` shows the `--port N` number of the DisplayPort output the adapter is plugged into. `probe` should report an **odd** capability (this adapter reports `0x07`).

## 4. Test

```bat
python C:\cec\dpcec.py standby --port 0
python C:\cec\dpcec.py on --port 0
```

If the screen goes away during a test, save the output to a file so you can read it afterwards:

```bat
python C:\cec\dpcec.py standby --port 0 > C:\cec\out.txt 2>&1
```

If the TV is stuck on "no signal", unplug the adapter from the PC for about 10 seconds and plug it back in.

### Result messages

| Output | Meaning |
|---|---|
| `OK: TV acknowledged` | Message delivered |
| `FAIL: TV did not answer (CEC off on TV?)` | No ACK from the TV. Check CEC is enabled in the TV's settings |
| `FAIL: TV rejected the message` | TV NACKed the data |
| `FAIL: CEC line error` | Problem on the CEC line |
| `FAIL: timeout` | No IRQ flag within 1 second |
| `AUX error: write 0xXXXX = YY: ...` | The AUX write to that address failed |

---

## 5. Automation: `C:\cec\install-tasks.bat`

Set `PORT`, save as `C:\cec\install-tasks.bat`, and **run it as administrator**. It uses `pythonw.exe`, so no console window pops up. It also removes the old "TV off at sleep" task if it exists.

```bat
@echo off
rem Run as administrator. Set PORT to the --port number from "dpcec.py list".
set PORT=0
set "PY="
for /f "delims=" %%i in ('where pythonw') do if not defined PY set "PY=%%i"
if not defined PY (echo pythonw.exe not found & pause & exit /b 1)
set "ON=\"%PY%\" C:\cec\dpcec.py on --port %PORT%"
set "OFF=\"%PY%\" C:\cec\dpcec.py standby --port %PORT%"
set "WATCH=\"%PY%\" C:\cec\dpcec.py watch --port %PORT%"

rem Old sleep task ran at wake instead of at sleep - remove it
schtasks /Delete /F /TN "CEC\TV off at sleep" >nul 2>&1

schtasks /Create /F /TN "CEC\TV on at logon"     /SC ONLOGON /TR "%ON%"
schtasks /Create /F /TN "CEC\TV on at wake"      /SC ONEVENT /EC System /MO "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]" /TR "%ON%"
schtasks /Create /F /TN "CEC\TV off at shutdown" /SC ONEVENT /EC System /MO "*[System[Provider[@Name='User32'] and EventID=1074]]" /TR "%OFF%"
schtasks /Create /F /TN "CEC\TV sleep watcher"   /SC ONLOGON /TR "%WATCH%"
rem Scheduled tasks are killed after 3 days by default; the watcher must run forever
powershell -NoProfile -Command "Set-ScheduledTask -TaskPath '\CEC\' -TaskName 'TV sleep watcher' -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero))"
schtasks /Run /TN "CEC\TV sleep watcher"
echo Done.
pause
```

### Tasks created (in Task Scheduler under the `CEC` folder)

| Task | Trigger | Action |
|---|---|---|
| TV on at logon | User logon | `on` |
| TV on at wake | Power-Troubleshooter, Event ID 1 (resume) | `on` |
| TV sleep watcher | User logon, runs in the background | `standby` just before the PC sleeps |
| TV off at shutdown | User32, Event ID 1074 (shutdown/restart initiated) | `standby` (untested: may also fire too late) |

### Logs

Scheduled runs write to `C:\cec\dpcec.log`. Each entry starts with a timestamp and the command. The watcher logs `watching for sleep` when it starts and `sleep -> standby` each time the PC goes to sleep.

---

# Part 2: Auto sign-in and Playnite fullscreen

## 6. Sign in automatically

Open a terminal **as administrator** and type `cmd` first. These commands are for Command Prompt: in PowerShell they fail with "Invalid key name", and PowerShell can drop the empty `""` password. Run `net user` to see the exact account name, then run these, replacing `Guest` with that name:

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v AutoAdminLogon /t REG_SZ /d 1 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultUserName /t REG_SZ /d "Guest" /f
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultPassword /t REG_SZ /d "" /f
```

Include the empty `DefaultPassword`, even though the account has no password: auto sign-in may not work without it.

So waking from sleep doesn't stop at the lock screen either, go to **Settings → Accounts → Sign-in options → "If you've been away, when should Windows require you to sign in again?"** and set it to **Never**.

To undo auto sign-in later:

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v AutoAdminLogon /t REG_SZ /d 0 /f
```

## 7. Start Playnite in fullscreen

Playnite comes with a separate program just for fullscreen mode, `Playnite.FullscreenApp.exe`. Put a shortcut to it in the Startup folder. **Do this while signed in to the account that signs in automatically**, because both the Startup folder and a default Playnite install belong to one account.

1. Press **Win + R**, type `shell:startup`, and press Enter.
2. Right-click → **New → Shortcut**, and enter this as the location:
   ```
   %LOCALAPPDATA%\Playnite\Playnite.FullscreenApp.exe
   ```
   That's the default install location. If you installed Playnite elsewhere, right-click your Playnite shortcut → **Open file location** and use the `Playnite.FullscreenApp.exe` in that folder.
3. In Playnite's settings, turn off its own "start with Windows" option if it's on. Otherwise desktop mode might open alongside fullscreen.

## 7b. Turn off UAC prompts

Press **Win + R**, type `UserAccountControlSettings`, drag the slider to the bottom (**Never notify**) and click OK. Or, in an admin `cmd`:

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v ConsentPromptBehaviorAdmin /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v PromptOnSecureDesktop /t REG_DWORD /d 0 /f
```

- Programs can then make admin-level changes without asking. That's the trade-off for no pop-ups on the TV.
- On an **administrator** account, admin requests are approved silently. On a **standard** account, they're refused silently, so emulators that need admin won't start elevated.
- **Emulators need admin, so the auto sign-in account must be an administrator:** Settings → Accounts → Other users → the account → **Change account type → Administrator**, then sign out and back in. Emulators set to "Run this program as an administrator" then start elevated with no prompt.
- With auto sign-in, no password and no UAC prompts, anything that runs on this PC has full control of it. Keep it a dedicated couch PC.
- Avoid turning UAC off completely (`EnableLUA = 0`): on Windows 10/11 it stops Microsoft Store apps from opening.
- To undo: move the slider back to the default (third notch).

## 8. Check the TV tasks

The CEC tasks from step 5 run under the account that was signed in when `install-tasks.bat` was run. If that's the account that now signs in automatically, nothing changes. If it's a different one, the TV on/off tasks won't run for that account as they are now.

## 9. Test

Restart the PC. It should go straight to the desktop, open Playnite fullscreen, and turn on the TV.

---

## Status / next steps

- [x] Install Python and gpu-aux
- [x] `list` / `probe`: capability `0x07`
- [x] `standby` and `on` reach the TV (`OK: TV acknowledged`)
- [x] Run the new `install-tasks.bat` as administrator
- [x] Sleep: TV turns off before the PC sleeps
- [x] Wake: TV turns on
- [ ] Shutdown: TV turns off
- [ ] Auto sign-in works after a restart
- [ ] Playnite opens in fullscreen at sign-in
- [ ] TV tasks run under the auto sign-in account
