"""Имитатор Quest для проверки приёмника без шлема.

Отвечает на маяк и SYNC_REQ, шлёт HAND_FRAME обеих рук (синтетическое движение), STATUS и EVENT,
выполняет команды start/stop. Часы Quest моделируются как perf_counter + смещение + дрейф,
что позволяет сравнить оценку синхронизации с известной истиной.

    python fake_quest.py --duration 20 --record-after 2 --record-for 10 --loss 0.01
"""
from __future__ import annotations

import argparse
import json
import math
import random
import socket
import time

from qhs import protocol as P

TRUE_OFFSET_S = 12345.678  # mono - pc в начале
TRUE_DRIFT = 25e-6  # 25 ppm
OVR_MINUS_MONO_S = 0.0  # на Android ожидаемо один и тот же CLOCK_MONOTONIC
CAPTURE_LATENCY_S = 0.012


class FakeQuest:
    def __init__(self, a):
        self.a = a
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", a.port))
        self.sock.setblocking(False)
        self.pc = (a.pc_ip, a.pc_port) if a.pc_ip else None
        self.seq = 0
        self.session = 0
        self.frame_index = [0, 0]
        self.pc0 = time.perf_counter()
        self.held: list[bytes] = []  # для имитации перестановок

    def mono(self, pc: float | None = None) -> float:
        pc = time.perf_counter() if pc is None else pc
        return pc + TRUE_OFFSET_S + TRUE_DRIFT * (pc - self.pc0)

    def send(self, msg_type: int, body: bytes = b"", obj: dict | None = None, lossy: bool = False) -> None:
        if not self.pc:
            return
        self.seq += 1
        pkt = P.build_json(msg_type, self.seq, obj) if obj is not None else P.build(msg_type, self.seq, body)
        if lossy and random.random() < self.a.loss:
            return
        if lossy and random.random() < self.a.reorder:
            self.held.append(pkt)
            return
        self.sock.sendto(pkt, self.pc)
        while self.held:
            self.sock.sendto(self.held.pop(), self.pc)

    def event(self, code: str, detail: str = "") -> None:
        m = self.mono()
        self.send(P.MSG_EVENT, obj={"code": code, "session": f"{self.session:016x}", "mono": m,
                                    "ovr": m + OVR_MINUS_MONO_S, "detail": detail})

    def start(self, label: str = "") -> None:
        if self.session:
            return
        self.session = random.getrandbits(64) | 1
        self.event("recording_started", f"label={label}")

    def stop(self) -> None:
        if not self.session:
            return
        self.event("recording_stopped")
        self.session = 0

    def poll(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, ConnectionResetError):
                return
            t2 = self.mono()
            try:
                hdr = P.parse_header(data)
            except P.ProtocolError:
                continue
            if hdr.type == P.MSG_BEACON:
                b = P.parse_json(data)
                if self.pc is None:
                    self.pc = (addr[0], b.get("pc_port", P.PC_PORT))
                    print("PC discovered:", self.pc)
            elif hdr.type == P.MSG_SYNC_REQ:
                sync_id, t1 = P.SYNC_REQ.unpack_from(data, P.HEADER_SIZE)
                self.pc = self.pc or addr
                self.seq += 1
                pkt = P.build_sync_resp(self.seq, sync_id, t1, t2, self.mono())
                self.sock.sendto(pkt, addr)
            elif hdr.type == P.MSG_COMMAND:
                c = P.parse_json(data)
                self.event("command_received", f"{c.get('cmd')} id={c.get('id')}")
                if c.get("cmd") == "start":
                    self.start(c.get("label", ""))
                elif c.get("cmd") == "stop":
                    self.stop()

    def hand_frame(self, hand: int, t: float) -> None:
        self.frame_index[hand] += 1
        mono_now = self.mono()
        ovr_now = mono_now + OVR_MINUS_MONO_S
        sample = ovr_now - CAPTURE_LATENCY_S
        bones = []
        for j in range(26):
            ang = 0.6 * math.sin(2 * math.pi * 0.5 * t + j * 0.2)
            bones.append((0.01 * j, 0.0, 0.02 * math.sin(ang), 0.0, math.sin(ang / 2), 0.0, math.cos(ang / 2)))
        f = P.HandFrame(0, self.session, self.frame_index[hand], hand, P.SOURCE_UNEXTRAPOLATED, 1, 26,
                        (1, 1, 1, 1, 1), P.FLAG_TRACKED | P.FLAG_INPUT_VALID | P.FLAG_POSITIONS | P.FLAG_ROTATIONS,
                        3, sample, sample, ovr_now, mono_now, int(t * 72), 1.0,
                        (0.2 * (hand * 2 - 1), 1.1, -0.3, 0, 0, 0, 1), bones)
        self.seq += 1
        pkt = P.build_hand_frame(self.seq, f)
        assert len(pkt) == 836, len(pkt)
        self.seq -= 1
        self.send(P.MSG_HAND_FRAME, pkt[P.HEADER_SIZE:], lossy=True)

    def status(self) -> None:
        m = self.mono()
        hs = {"rate_hz": self.a.rate, "unextrapolated_ok": True, "tracked": True, "high_confidence": True,
              "positions": True, "bones": 26, "source": 1}
        self.send(P.MSG_STATUS, obj={"app_version": "fake", "device": "FakeQuest", "session": f"{self.session:016x}",
                                     "recording": bool(self.session), "label": "", "mono": m, "ovr": m,
                                     "left": hs, "right": hs})

    def run(self) -> None:
        t0 = time.perf_counter()
        period = 1.0 / self.a.rate
        next_frame = t0
        next_status = t0
        while True:
            t = time.perf_counter() - t0
            if t > self.a.duration:
                break
            self.poll()
            if self.a.record_after >= 0 and not self.session and self.a.record_after <= t < self.a.record_after + 0.1:
                self.start("fake")
            if self.session and self.a.record_for > 0 and t > self.a.record_after + self.a.record_for:
                self.stop()
                self.a.record_for = -1
            now = time.perf_counter()
            if now >= next_frame:
                next_frame += period
                self.hand_frame(0, t)
                self.hand_frame(1, t)
            if now >= next_status:
                next_status += 1.0
                self.status()
            time.sleep(0.0005)
        self.stop()
        print(json.dumps({"true_offset_s": TRUE_OFFSET_S, "true_drift_ppm": TRUE_DRIFT * 1e6,
                          "pc0_perf_s": self.pc0, "frames": self.frame_index, "seq": self.seq}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=P.QUEST_PORT)
    ap.add_argument("--pc-ip", default="", help="IP ПК (по умолчанию — из маяка)")
    ap.add_argument("--pc-port", type=int, default=P.PC_PORT)
    ap.add_argument("--rate", type=float, default=60.0, help="Гц на руку")
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--record-after", type=float, default=2.0, help="начать запись через N с (-1 — не начинать)")
    ap.add_argument("--record-for", type=float, default=10.0)
    ap.add_argument("--loss", type=float, default=0.01)
    ap.add_argument("--reorder", type=float, default=0.005)
    FakeQuest(ap.parse_args()).run()


if __name__ == "__main__":
    main()
