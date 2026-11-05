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
    # No LA mask (0x300E): ADL refuses that write, and it's only needed to receive, not to send on/standby

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
