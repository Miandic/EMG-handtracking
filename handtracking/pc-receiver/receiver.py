"""Приёмник потока рук Quest -> ПК.

Запуск:  python receiver.py            (автопоиск: ПК рассылает маяк, Quest подключается сам)
Команды в консоли:  r [метка] — начать запись,  s — остановить,  q — выход.
Запись также можно начать кнопкой на панели в Quest.

Сессия сохраняется в recordings/<дата>_<session>/pc.qhslog — исходные пакеты с метками
perf_counter_ns. Разбор и пересчёт времени: python convert.py <папка сессии>.
"""
from __future__ import annotations

import argparse
import collections
import ipaddress
import os
import platform
import queue
import select
import socket
import statistics
import sys
import threading
import time

from qhs import protocol as P
from qhs.clocksync import SyncSample, fit, fit_ovr_mono
from qhs.logfile import DIR_RECEIVED, DIR_SENT, LogWriter

RECEIVER_VERSION = "0.1.0"
DIR_LOCAL = 2  # заметка приёмника (EVENT, сформированный на ПК)
STREAM_GAP_S = 1.0


def now_ns() -> int:
    return time.perf_counter_ns()


def broadcast_addresses() -> list[str]:
    """255.255.255.255 + широковещательные адреса /24 для локальных IPv4 (Windows шлёт глобальный broadcast
    только через один интерфейс)."""
    out = {"255.255.255.255"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                out.add(str(ipaddress.ip_network(ip + "/24", strict=False).broadcast_address))
    except OSError:
        pass
    return sorted(out)


class Session:
    def __init__(self, out_dir: str, session_hex: str, quest_addr, label: str):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.dir = os.path.join(out_dir, f"{stamp}_{session_hex}")
        self.session_hex = session_hex
        meta = {
            "format": "qhslog",
            "protocol_version": P.VERSION,
            "writer": "pc",
            "receiver_version": RECEIVER_VERSION,
            "session": session_hex,
            "label": label,
            "created_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "created_unix_s": time.time(),
            "perf_counter_ns_at_create": now_ns(),
            "time_ns_domain": "pc time.perf_counter_ns",
            "host": platform.node(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "quest_addr": f"{quest_addr[0]}:{quest_addr[1]}" if quest_addr else "",
        }
        self.log = LogWriter(os.path.join(self.dir, "pc.qhslog"), meta)
        self.started = time.monotonic()

    def close(self):
        self.log.close()


class Receiver:
    def __init__(self, args):
        self.args = args
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind(("0.0.0.0", args.port))
        self.seq = 0
        self.quest_addr = (args.quest_ip, args.quest_port) if args.quest_ip else None
        self.bcast = broadcast_addresses()
        self.session: Session | None = None
        self.closed_sessions: set[str] = set()
        self.cmd_queue: queue.Queue[str] = queue.Queue()
        self.pending_cmd: dict | None = None
        self.cmd_id = 0
        self.sync_id = 0
        self.sync_samples: collections.deque[SyncSample] = collections.deque(maxlen=600)
        self.ovr_pairs: collections.deque[tuple[float, float]] = collections.deque(maxlen=2000)
        self.last_status: dict = {}
        self.events: collections.deque[str] = collections.deque(maxlen=5)
        # статистика за секунду и накопленная
        self.expected_seq: int | None = None
        self.lost_total = 0
        self.reordered_total = 0
        self.frames_sec = [0, 0]
        self.latency_sec: list[float] = []
        self.last_frame_t = [0.0, 0.0]
        self.last_any_frame = 0.0
        self.gap_reported = False
        self.running = True
        self._model = None
        self._ovr_rel = None
        self._model_t = 0.0

    # ---------- отправка ----------

    def send(self, data: bytes, addr) -> None:
        try:
            self.sock.sendto(data, addr)
        except OSError as e:
            self.note(f"send error to {addr}: {e}")
            return
        if self.session and data[4] != P.MSG_BEACON:
            self.session.log.write(DIR_SENT, now_ns(), data)

    def next_seq(self) -> int:
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        return self.seq

    def send_beacon(self) -> None:
        pkt = P.build_json(P.MSG_BEACON, self.next_seq(), {"pc_port": self.args.port, "name": self.args.name})
        targets = [(b, self.args.quest_port) for b in self.bcast]
        if self.quest_addr:
            targets.append(self.quest_addr)
        for t in targets:
            try:
                self.sock.sendto(pkt, t)
            except OSError:
                pass

    def send_sync(self) -> None:
        if not self.quest_addr:
            return
        self.sync_id += 1
        self.send(P.build_sync_req(self.next_seq(), self.sync_id, now_ns()), self.quest_addr)

    def send_command(self, cmd: str, label: str = "") -> None:
        if not self.quest_addr:
            print("Quest ещё не подключён — команда не отправлена")
            return
        self.cmd_id += 1
        self.pending_cmd = {"cmd": cmd, "id": self.cmd_id, "label": label, "tries": 0, "next": 0.0}

    def pump_command(self, t: float) -> None:
        c = self.pending_cmd
        if not c or t < c["next"]:
            return
        if c["tries"] >= 5:
            self.note(f"команда {c['cmd']} id={c['id']} не подтверждена Quest")
            self.pending_cmd = None
            return
        c["tries"] += 1
        c["next"] = t + 0.3
        self.send(P.build_json(P.MSG_COMMAND, self.next_seq(), {"cmd": c["cmd"], "id": c["id"], "label": c["label"]}),
                  self.quest_addr)

    # ---------- приём ----------

    def note(self, text: str) -> None:
        """Событие приёмника: в консоль и (если идёт запись) в журнал сессии."""
        line = time.strftime("%H:%M:%S ") + text
        self.events.append(line)
        print("\n" + line)
        if self.session:
            pkt = P.build_json(P.MSG_EVENT, 0, {"code": "pc_note", "detail": text, "pc_ns": now_ns()})
            self.session.log.write(DIR_LOCAL, now_ns(), pkt)

    def handle(self, data: bytes, addr, t_ns: int) -> None:
        try:
            hdr = P.parse_header(data)
        except P.ProtocolError:
            return
        if hdr.type in (P.MSG_BEACON, P.MSG_SYNC_REQ, P.MSG_COMMAND):
            return  # свои/чужие пакеты ПК (broadcast может вернуться на этот же сокет)

        if self.quest_addr is None or self.quest_addr[0] != addr[0]:
            self.quest_addr = (addr[0], addr[1])
            self.note(f"Quest: {addr[0]}:{addr[1]}")
            self.expected_seq = None

        self.track_seq(hdr.seq)
        session_hex = None

        if hdr.type == P.MSG_HAND_FRAME:
            f = P.parse_hand_frame(data, hdr.seq)
            self.on_frame(f, t_ns)
            session_hex = f"{f.session_id:016x}" if f.session_id else None
        elif hdr.type == P.MSG_SYNC_RESP:
            r = P.parse_sync_resp(data, hdr.seq)
            self.sync_samples.append(SyncSample(r.pc_t1_ns / 1e9, r.q_t2, r.q_t3, t_ns / 1e9))
        elif hdr.type == P.MSG_STATUS:
            self.last_status = P.parse_json(data)
            if self.last_status.get("recording"):
                session_hex = self.last_status.get("session")
        elif hdr.type == P.MSG_EVENT:
            ev = P.parse_json(data)
            code = ev.get("code", "")
            self.note(f"Quest event: {code} {ev.get('detail', '')}")
            if code == "command_received" and self.pending_cmd and f"id={self.pending_cmd['id']}" in ev.get("detail", ""):
                self.pending_cmd = None
            if code == "recording_started":
                session_hex = ev.get("session")

        if session_hex and session_hex not in self.closed_sessions and (
                self.session is None or self.session.session_hex != session_hex):
            self.open_session(session_hex)
        if self.session:
            self.session.log.write(DIR_RECEIVED, t_ns, data)
        if hdr.type == P.MSG_EVENT and P.parse_json(data).get("code") == "recording_stopped":
            self.close_session("остановлена на Quest")
        elif hdr.type == P.MSG_STATUS and self.session and not self.last_status.get("recording"):
            self.close_session("Quest сообщает: запись не идёт")

    def track_seq(self, seq: int) -> None:
        if self.expected_seq is None:
            self.expected_seq = seq + 1
            return
        diff = (seq - self.expected_seq) & 0xFFFFFFFF
        if diff == 0:
            self.expected_seq = seq + 1
        elif diff < 0x80000000:
            if diff > 100000:  # перезапуск приложения на Quest
                self.note("seq reset (перезапуск приложения Quest?)")
            else:
                self.lost_total += diff
            self.expected_seq = seq + 1
        else:
            self.reordered_total += 1  # опоздавший пакет; он уже учтён как потерянный
            self.lost_total = max(0, self.lost_total - 1)

    def on_frame(self, f: P.HandFrame, t_ns: int) -> None:
        t = time.monotonic()
        self.frames_sec[f.hand] += 1
        self.last_frame_t[f.hand] = t
        self.last_any_frame = t
        if self.gap_reported:
            self.gap_reported = False
            self.note("поток рук восстановлен")
        self.ovr_pairs.append((f.ovr_now, f.mono_now))
        model, rel = self.model, self._ovr_rel
        if model and rel:
            sample_pc = model.mono_to_pc(rel.ovr_to_mono(f.sample_time))
            self.latency_sec.append(t_ns / 1e9 - sample_pc)

    @property
    def model(self):
        """Модель часов, пересчитывается не чаще 2 раз в секунду."""
        t = time.monotonic()
        if t - self._model_t > 0.5:
            self._model_t = t
            self._model = fit(list(self.sync_samples)) if len(self.sync_samples) >= 3 else None
            self._ovr_rel = fit_ovr_mono(list(self.ovr_pairs)[-200:]) if len(self.ovr_pairs) > 5 else None
        return self._model

    # ---------- сессии ----------

    def open_session(self, session_hex: str) -> None:
        if self.session:
            self.close_session("новая сессия")
        label = self.last_status.get("label", "") if self.last_status else ""
        self.session = Session(self.args.out, session_hex, self.quest_addr, label)
        self.note(f"запись: {self.session.dir}")

    def close_session(self, reason: str) -> None:
        if not self.session:
            return
        s = self.session
        self.closed_sessions.add(s.session_hex)  # опоздавшие пакеты этой сессии не открывают её заново
        self.note(f"запись закрыта ({reason}): {s.log.records} пакетов, {s.log.bytes_written / 1e6:.1f} МБ")
        self.session = None
        s.close()

    # ---------- цикл ----------

    def stdin_loop(self) -> None:
        for line in sys.stdin:
            self.cmd_queue.put(line.strip())

    def handle_console(self) -> None:
        while not self.cmd_queue.empty():
            line = self.cmd_queue.get()
            if not line:
                continue
            cmd, _, rest = line.partition(" ")
            if cmd == "r":
                self.send_command("start", rest.strip())
            elif cmd == "s":
                self.send_command("stop")
            elif cmd == "q":
                self.running = False
            else:
                print("команды: r [метка] — запись, s — стоп, q — выход")

    def print_stats(self) -> None:
        st = self.last_status or {}
        m = self.model
        lat = statistics.median(self.latency_sec) * 1e3 if self.latency_sec else float("nan")
        hands = []
        for h, key in ((0, "left"), (1, "right")):
            hs = st.get(key, {}) or {}
            flag = "" if hs.get("unextrapolated_ok", False) else "!"
            trk = "T" if hs.get("tracked") else "-"
            hands.append(f"{'LR'[h]}:{self.frames_sec[h]:3d}Гц{trk}{flag}")
        rec = f"REC {self.session.session_hex[:8]} {self.session.log.bytes_written / 1e6:6.1f}МБ" if self.session else "--- "
        sync = f"θ={m.a * 1e3:+.2f}мс rtt={m.rtt_min * 1e3:.1f}мс ±{m.uncertainty_s * 1e3:.1f}" if m else "sync: нет"
        q = f"{self.quest_addr[0]}" if self.quest_addr else "Quest не найден"
        line = (f"{q} | {' '.join(hands)} | потери {self.lost_total} перест. {self.reordered_total} | "
                f"задержка {lat:5.1f}мс | {sync} | {rec}")
        print("\r" + line[:200].ljust(160), end="", flush=True)
        self.frames_sec = [0, 0]
        self.latency_sec = []

    def run(self) -> None:
        print(f"Приёмник QHS1 v{RECEIVER_VERSION}: порт {self.args.port}, маяк -> {', '.join(self.bcast)}:{self.args.quest_port}")
        print("Команды: r [метка] — начать запись, s — остановить, q — выход\n")
        threading.Thread(target=self.stdin_loop, daemon=True).start()
        t_beacon = t_sync = t_stats = 0.0
        t_start = time.monotonic()
        sync_period = 1.0 / self.args.sync_hz
        try:
            while self.running:
                r, _, _ = select.select([self.sock], [], [], 0.01)
                if r:
                    for _ in range(256):
                        try:
                            data, addr = self.sock.recvfrom(2048)
                        except (BlockingIOError, ConnectionResetError):
                            break
                        t_ns = now_ns()
                        try:
                            self.handle(data, addr, t_ns)
                        except Exception as e:  # повреждённый пакет не должен останавливать запись
                            self.note(f"ошибка разбора пакета от {addr}: {e!r}")
                        r2, _, _ = select.select([self.sock], [], [], 0)
                        if not r2:
                            break
                t = time.monotonic()
                if self.args.duration and t - t_start > self.args.duration:
                    break
                if not self.args.no_beacon and t >= t_beacon:
                    t_beacon = t + 1.0
                    self.send_beacon()
                if t >= t_sync:
                    t_sync = t + sync_period
                    self.send_sync()
                self.pump_command(t)
                self.handle_console()
                if self.last_any_frame and not self.gap_reported and t - self.last_any_frame > STREAM_GAP_S:
                    self.gap_reported = True
                    self.note(f"нет кадров рук более {STREAM_GAP_S:.0f} с")
                if t >= t_stats:
                    t_stats = t + 1.0
                    if self.session:
                        self.session.log.flush()
                    self.print_stats()
        except KeyboardInterrupt:
            pass
        finally:
            self.close_session("выход из приёмника")
            self.sock.close()
            print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=P.PC_PORT, help="порт приёма на ПК")
    ap.add_argument("--quest-port", type=int, default=P.QUEST_PORT, help="порт Quest")
    ap.add_argument("--quest-ip", default="", help="IP Quest (если широковещательный маяк не проходит)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings"))
    ap.add_argument("--name", default=platform.node(), help="имя ПК в маяке")
    ap.add_argument("--sync-hz", type=float, default=5.0, help="частота обмена синхронизации")
    ap.add_argument("--no-beacon", action="store_true")
    ap.add_argument("--duration", type=float, default=0, help="завершить через N с (для тестов)")
    args = ap.parse_args()
    Receiver(args).run()


if __name__ == "__main__":
    main()
