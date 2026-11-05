# dpcec_diag.py - figure out why DPCD writes fail (AMD / gpu_aux)
# Usage: python C:\cec\dpcec_diag.py --port 0
import argparse
from gpu_aux import AuxPort, AuxError

ap = argparse.ArgumentParser()
ap.add_argument("--gpu", type=int, default=0)
ap.add_argument("--port", type=int, default=0)
a = ap.parse_args()

def step(name, fn):
    try:
        r = fn()
        print(f"[ OK ] {name}" + (f": {r.hex(' ').upper()}" if isinstance(r, bytes) else ""))
        return r
    except AuxError as e:
        print(f"[FAIL] {name}: {e}")

with AuxPort("DP", index=a.port, gpu_index=a.gpu, backend="AMD") as p:
    # Reads
    step("read DPCD rev 0x0000", lambda: p.read_dpcd(0x0000, 1))
    step("read CEC regs 0x3000-0x300F", lambda: p.read_dpcd(0x3000, 16))

    # Test 1: harmless write outside the CEC range.
    # 0x0300-0x0302 is the Source OUI, which the PC is allowed to write.
    # We write back exactly what's already there, so nothing changes.
    oui = step("read Source OUI 0x0300", lambda: p.read_dpcd(0x0300, 3))
    if oui is not None:
        step("write same value back to 0x0300 (general write test)", lambda: p.write_dpcd(0x0300, oui))

    # Test 2: the CEC writes the real script needs
    step("write 0x3001 = 01 (enable tunneling)", lambda: p.write_dpcd(0x3001, b"\x01"))
    step("read back 0x3001", lambda: p.read_dpcd(0x3001, 1))
    step("write 0x300E = 10 00 (LA mask)", lambda: p.write_dpcd(0x300E, b"\x10\x00"))
    step("write 0x3004 = 00 (IRQ, no-op)", lambda: p.write_dpcd(0x3004, b"\x00"))
