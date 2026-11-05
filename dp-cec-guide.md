# HDMI-CEC from an AMD GPU on Windows through a DP→HDMI adapter

Turn the TV on at logon/wake and put it in standby at sleep/shutdown, from a Windows PC whose TV is connected through a DisplayPort→HDMI adapter.

> **Status:** not yet tested on Windows. CEC through this adapter is confirmed working on Bazzite (Linux).

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
- CEC worked on Bazzite (Linux), which shows the UGREEN adapter supports **DisplayPort 1.3 "CEC tunneling over AUX"**. Linux's `amdgpu` driver exposes this; Windows doesn't.
- The workaround: drive the adapter's CEC registers (**DPCD 0x3000–0x30FF**) directly over the DP AUX channel from user space.
- AMD's AUX function (`ADL_Display_NativeAUXChannel_Access` in `atiadlxx.dll`) is officially NDA-only, but the open-source Python library **gpu_aux** (<https://github.com/cnbright/gpu_aux>, `pip install gpu-aux`) has reverse-engineered it and provides `read_dpcd` / `write_dpcd` / `i2c_read` / `i2c_write` for AMD.

### What the script does

1. Checks the tunneling capability register (`0x3000`).
2. Enables CEC tunneling (`0x3001`).
3. Sets the logical address mask (`0x300E`) to Playback Device 1 (logical address 4).
4. Writes the CEC frame to the TX buffer (`0x3020`).
5. Triggers transmission via `0x3003`.
6. Polls the IRQ flags (`0x3004`) for success or error.

Messages sent:

| Command | CEC message | Frame |
|---|---|---|
| `on` | Image View On (to TV) | `40 04` |
| `on --pa XXXX` | + Active Source (broadcast) | `4F 82 XX XX` |
| `standby` | Standby (to TV) | `40 36` |

It can also read the TV's HDMI physical address from the EDID (`pa` command).

### Caveat

The ADL AUX interface is private and undocumented. An Adrenalin driver update could break it.

On this setup ADL accepts 1-byte DPCD writes but fails multi-byte ones with `ADL error -1`, so the script writes every register one byte at a time (`wr()` helper).

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

Commands: `list` | `probe` | `pa` | `on` | `standby` (options: `--gpu N`, `--port N`, `--pa XXXX`)

```python
# dpcec.py - HDMI-CEC through a DP->HDMI adapter (AMD GPU, Windows)
# Commands: list | probe | pa | on | standby   (add --port N, --pa XXXX)
import argparse, sys, time
from gpu_aux import AuxPort, AuxError, enumerate_gpus, enumerate_ports

if sys.stdout is None:
    sys.stdout = sys.stderr = open(r"C:\cec\dpcec.log", "a")

CAP, CTL, TX_INFO, IRQ = 0x3000, 0x3001, 0x3003, 0x3004
LA_MASK, TX_BUF = 0x300E, 0x3020
TX_SENT, LINE_ERR, ADDR_NACK, DATA_NACK, TX_ALL = 0x10, 0x20, 0x40, 0x80, 0xF0
LA = 4

def wr(p, addr, data):
    # AMD ADL rejects multi-byte native AUX writes (ADL -1), so write one byte at a time
    for i, b in enumerate(data):
        p.write_dpcd(addr + i, bytes([b]))

def list_ports():
    for gi, gpu in enumerate(enumerate_gpus("AMD")):
        print(f"GPU {gi}: {gpu.name}")
        counts = {}
        for port in enumerate_ports("AMD", gi):
            idx = counts.get(port.kind, 0); counts[port.kind] = idx + 1
            print(f"  {port.kind} --port {idx}   {port.name}")

def read_pa(p):
    edid = b""
    for off in (0x00, 0x80):
        p.i2c_write(0xA0, bytes([off]))
        edid += p.i2c_read(0xA0, 128)
    ext = edid[128:]
    if edid[126] < 1 or ext[0] != 0x02:
        sys.exit("No CEA extension block in the TV's EDID")
    i, end = 4, ext[2]
    while i < end:
        tag, length = ext[i] >> 5, ext[i] & 0x1F
        if tag == 3 and ext[i+1:i+4] == b"\x03\x0c\x00":
            pa = (ext[i+4] << 8) | ext[i+5]
            print(f"Physical address {pa>>12}.{(pa>>8)&15}.{(pa>>4)&15}.{pa&15}  ->  use --pa {pa:04x}")
            return
        i += length + 1
    sys.exit("No HDMI physical address found in EDID")

def cec_enable(p):
    cap = p.read_dpcd(CAP, 1)[0]
    print(f"CEC tunneling capability: 0x{cap:02x}")
    if not cap & 0x01:
        sys.exit("This adapter/port does not report CEC tunneling")
    wr(p, CTL, b"\x01")
    m = 1 << LA
    wr(p, LA_MASK, bytes([m & 0xFF, m >> 8]))

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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "probe", "pa", "on", "standby"])
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--pa")
    a = ap.parse_args()
    print(time.strftime("%Y-%m-%d %H:%M:%S"), a.cmd)
    if a.cmd == "list":
        return list_ports()
    ok = True
    with AuxPort("DP", index=a.port, gpu_index=a.gpu, backend="AMD") as p:
        if a.cmd == "pa":
            return read_pa(p)
        cec_enable(p)
        if a.cmd == "on":
            ok = cec_send(p, [(LA << 4) | 0x0, 0x04])
            if ok and a.pa:
                pa = int(a.pa, 16)
                time.sleep(0.2)
                ok = cec_send(p, [(LA << 4) | 0xF, 0x82, pa >> 8, pa & 0xFF])
        elif a.cmd == "standby":
            ok = cec_send(p, [(LA << 4) | 0x0, 0x36])
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    try:
        main()
    except AuxError as e:
        sys.exit(f"AUX error: {e}")
```

---

## 3. Find the port

```bat
python C:\cec\dpcec.py list
```

Note the `--port N` number of the DisplayPort output the adapter is plugged into.

## 4. Check the adapter

```bat
python C:\cec\dpcec.py probe --port 0
```

The reported capability should be **odd** (bit 0 set), e.g. `0x01`. If it's even, that adapter/port doesn't report CEC tunneling.

```bat
python C:\cec\dpcec.py pa --port 0
```

This reads the TV's HDMI physical address from the EDID and prints the value to use with `--pa` (e.g. `1000` for `1.0.0.0`).

## 5. Test

```bat
python C:\cec\dpcec.py standby --port 0
python C:\cec\dpcec.py on --port 0 --pa 1000
```

Replace `0` and `1000` with your values from steps 3–4.

### Result messages

| Output | Meaning |
|---|---|
| `OK: TV acknowledged` | Message delivered |
| `FAIL: TV did not answer (CEC off on TV?)` | No ACK from the TV — check CEC is enabled in the TV's settings |
| `FAIL: TV rejected the message` | TV NACKed the data |
| `FAIL: CEC line error` | Problem on the CEC line |
| `FAIL: timeout` | No IRQ flag within 1 second |
| `AUX error: ...` | The AUX access itself failed (wrong port, or gpu_aux/driver issue) |

---

## 6. Automation: `C:\cec\install-tasks.bat`

Set `PORT` and `PA` to your values, save as `C:\cec\install-tasks.bat`, and **run it as administrator**. It uses `pythonw.exe` so no console window pops up.

```bat
@echo off
set PORT=0
set PA=1000
set "PY="
for /f "delims=" %%i in ('where pythonw') do if not defined PY set "PY=%%i"
if not defined PY (echo pythonw.exe not found & pause & exit /b 1)
set "ON=\"%PY%\" C:\cec\dpcec.py on --port %PORT% --pa %PA%"
set "OFF=\"%PY%\" C:\cec\dpcec.py standby --port %PORT%"
schtasks /Create /F /TN "CEC\TV on at logon"    /SC ONLOGON /TR "%ON%"
schtasks /Create /F /TN "CEC\TV on at wake"     /SC ONEVENT /EC System /MO "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]" /TR "%ON%"
schtasks /Create /F /TN "CEC\TV off at sleep"   /SC ONEVENT /EC System /MO "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=42]]" /TR "%OFF%"
schtasks /Create /F /TN "CEC\TV off at shutdown" /SC ONEVENT /EC System /MO "*[System[Provider[@Name='User32'] and EventID=1074]]" /TR "%OFF%"
echo Done.
pause
```

### Tasks created (in Task Scheduler under the `CEC` folder)

| Task | Trigger | Action |
|---|---|---|
| TV on at logon | User logon | `on` |
| TV on at wake | Power-Troubleshooter, Event ID 1 (resume) | `on` |
| TV off at sleep | Kernel-Power, Event ID 42 (entering sleep) | `standby` |
| TV off at shutdown | User32, Event ID 1074 (shutdown/restart initiated) | `standby` |

### Logs

Scheduled runs (via `pythonw`) write their output to:

```
C:\cec\dpcec.log
```

---

## Status / next steps

- [ ] Install Python and gpu-aux
- [ ] `list` — find the port
- [ ] `probe` — confirm tunneling capability is odd
- [ ] `pa` — get the physical address
- [ ] Test `standby` and `on` manually
- [ ] Run `install-tasks.bat` as administrator
- [ ] Verify logon, wake, sleep and shutdown behavior using `C:\cec\dpcec.log`
