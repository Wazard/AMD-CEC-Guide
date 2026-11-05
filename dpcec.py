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
