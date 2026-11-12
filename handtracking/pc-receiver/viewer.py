"""3D-просмотрщик записей рук (.qhslog) — только стандартная библиотека Python (tkinter).

    python viewer.py                          диалог выбора файла
    python viewer.py recordings/<сессия>      папка сессии (pc.qhslog)
    python viewer.py файл.qhslog              журнал ПК или резервная копия с Quest

Мышь: левая кнопка — вращение, правая — сдвиг, колесо — масштаб.
Клавиши: Пробел — пуск/пауза, ←/→ — кадр (Shift — 1 с), Home/End — начало/конец, +/− — скорость,
         A — оси суставов, L — подписи, T — след кончиков пальцев, G — сетка пола,
         1 — вид от пользователя, 2 — сверху, 3 — сбоку, R — сброс вида.

Координаты — исходные OVRPlugin (пространство трекинга, метры, ось Y вверх, −Z — вперёд от шлема).
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import struct
import sys
import time
import tkinter as tk
from array import array
from tkinter import filedialog, messagebox, ttk

from qhs import protocol as P
from qhs.logfile import DIR_RECEIVED, read_log

# --- скелет OpenXR (XrHandJointEXT) ---
PALM, WRIST = 0, 1
FINGERS = {
    "thumb": [2, 3, 4, 5],
    "index": [6, 7, 8, 9, 10],
    "middle": [11, 12, 13, 14, 15],
    "ring": [16, 17, 18, 19, 20],
    "little": [21, 22, 23, 24, 25],
}
TIPS = [5, 10, 15, 20, 25]
EDGES: list[tuple[int, int]] = []
for chain in FINGERS.values():
    EDGES.append((WRIST, chain[0]))
    EDGES += list(zip(chain, chain[1:]))
EDGES += [(7, 12), (12, 17), (17, 22)]  # линия пястно-фаланговых суставов

HAND_COLORS = {0: "#4ea1ff", 1: "#ff9a3c"}
HAND_NAMES = {0: "Левая", 1: "Правая"}
STALE_S = 0.1  # кадр старше — «нет данных»
BG = "#14161a"

_OFF_HAND = 24
_OFF_SAMPLE = 40
_FIXED = P.HAND_FIXED


class Recording:
    """Кадры обеих рук: время захвата (ovr) + исходные байты пакета; разбор — по требованию."""

    def __init__(self, path: str):
        if os.path.isdir(path):
            path = os.path.join(path, "pc.qhslog")
        self.path = path
        self.meta, records = read_log(path)
        writer = self.meta.get("writer", "pc")
        self.times = {0: array("d"), 1: array("d")}
        self.packets: dict[int, list[bytes]] = {0: [], 1: []}
        self.events: list[tuple[float, str, str]] = []
        for rec in records:
            if writer == "pc" and rec.direction != DIR_RECEIVED:
                continue
            if writer == "quest" and rec.direction != 1:
                continue
            d = rec.data
            if len(d) < P.HEADER_SIZE or d[:4] != P.MAGIC:
                continue
            t = d[4]
            if t == P.MSG_HAND_FRAME and len(d) >= P.HAND_FIXED_SIZE:
                hand = d[_OFF_HAND]
                if hand in (0, 1):
                    self.times[hand].append(struct.unpack_from("<d", d, _OFF_SAMPLE)[0])
                    self.packets[hand].append(d)
            elif t == P.MSG_EVENT:
                try:
                    e = P.parse_json(d)
                    if "ovr" in e:
                        self.events.append((float(e["ovr"]), e.get("code", ""), e.get("detail", "")))
                except (ValueError, KeyError):
                    pass
        # сортировка по времени захвата (на случай перестановок пакетов)
        for h in (0, 1):
            if any(b < a for a, b in zip(self.times[h], self.times[h][1:])):
                order = sorted(range(len(self.times[h])), key=self.times[h].__getitem__)
                self.times[h] = array("d", (self.times[h][i] for i in order))
                self.packets[h] = [self.packets[h][i] for i in order]
        self.events.sort()
        all_t = [ts[0] for ts in self.times.values() if ts] + [ts[-1] for ts in self.times.values() if ts]
        if not all_t:
            raise ValueError("в журнале нет кадров рук")
        self.t0, self.t1 = min(all_t), max(all_t)
        self._cache: dict[tuple[int, int], P.HandFrame] = {}

    @property
    def duration(self) -> float:
        return self.t1 - self.t0

    def frame_count(self, hand: int) -> int:
        return len(self.times[hand])

    def index_at(self, hand: int, t: float) -> int:
        """Индекс последнего кадра с временем захвата ≤ t (−1, если нет)."""
        return bisect.bisect_right(self.times[hand], t) - 1

    def frame(self, hand: int, i: int) -> P.HandFrame:
        key = (hand, i)
        f = self._cache.get(key)
        if f is None:
            d = self.packets[hand][i]
            f = P.parse_hand_frame(d, struct.unpack_from("<I", d, 8)[0])
            if len(self._cache) > 4000:
                self._cache.clear()
            self._cache[key] = f
        return f

    def center(self) -> tuple[float, float, float]:
        pts = []
        for h in (0, 1):
            n = self.frame_count(h)
            for i in range(0, n, max(1, n // 200)):
                f = self.frame(h, i)
                if f.tracked:
                    pts.append(f.root[:3])
        if not pts:
            return (0.0, 1.0, -0.4)
        return tuple(sum(c) / len(pts) for c in zip(*pts))  # type: ignore[return-value]


# --- математика ---

def quat_rotate(q: tuple[float, float, float, float], v: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z, w = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v); v' = v + w*t + cross(q.xyz, t)
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


class Camera:
    def __init__(self):
        self.target = [0.0, 1.0, -0.4]
        self.yaw = 0.0
        self.pitch = math.radians(15)
        self.dist = 0.9
        self._basis()

    def _basis(self):
        cp = math.cos(self.pitch)
        off = (cp * math.sin(self.yaw), math.sin(self.pitch), cp * math.cos(self.yaw))
        self.pos = [self.target[i] + self.dist * off[i] for i in range(3)]
        f = [-o for o in off]
        r = (f[1] * 0 - f[2] * 1, f[2] * 0 - f[0] * 0, f[0] * 1 - f[1] * 0)  # cross(f, up)
        n = math.hypot(*r) or 1.0
        r = tuple(c / n for c in r)
        u = (r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0])
        self.f, self.r, self.u = f, r, u

    def update(self):
        self.pitch = max(-1.55, min(1.55, self.pitch))
        self.dist = max(0.1, min(10.0, self.dist))
        self._basis()

    def project(self, p, w: int, h: int):
        d = (p[0] - self.pos[0], p[1] - self.pos[1], p[2] - self.pos[2])
        z = d[0] * self.f[0] + d[1] * self.f[1] + d[2] * self.f[2]
        if z < 0.02:
            return None
        x = d[0] * self.r[0] + d[1] * self.r[1] + d[2] * self.r[2]
        y = d[0] * self.u[0] + d[1] * self.u[1] + d[2] * self.u[2]
        foc = 0.9 * h
        return (w / 2 + foc * x / z, h / 2 - foc * y / z, z)

    def depth(self, p) -> float:
        return ((p[0] - self.pos[0]) * self.f[0] + (p[1] - self.pos[1]) * self.f[1]
                + (p[2] - self.pos[2]) * self.f[2])

    def project_segment(self, a, b, w: int, h: int, near: float = 0.05):
        """Проекция отрезка с отсечением по ближней плоскости."""
        da, db = self.depth(a), self.depth(b)
        if da < near and db < near:
            return None
        if da < near or db < near:
            k = (near - da) / (db - da)
            m = tuple(a[i] + (b[i] - a[i]) * k for i in range(3))
            a, b = (m, b) if da < near else (a, m)
        pa, pb = self.project(a, w, h), self.project(b, w, h)
        return (pa, pb) if pa and pb else None


# --- окно ---

class Viewer:
    SPEEDS = [0.1, 0.25, 0.5, 1.0, 2.0, 4.0]

    def __init__(self, root: tk.Tk, rec: Recording):
        self.root = root
        self.rec = rec
        self.cam = Camera()
        self.reset_view()
        self.t = rec.t0
        self.playing = False
        self.speed_i = 3
        self.show_axes = False
        self.show_labels = False
        self.show_trail = True
        self.show_grid = True
        self._last_wall = time.perf_counter()
        self._drag = None
        self._dirty = True
        self._slider_lock = False

        label = rec.meta.get("label") or ""
        root.title(f"Hand Viewer — {os.path.basename(os.path.dirname(rec.path)) or rec.path} {label}")
        root.configure(bg=BG)
        root.geometry("1200x800")

        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        bar = tk.Frame(root, bg="#1d2026")
        bar.pack(fill="x")
        self.play_btn = tk.Button(bar, text="▶", width=3, command=self.toggle_play, bg="#2a2f38", fg="white",
                                  relief="flat", activebackground="#3a4250", activeforeground="white")
        self.play_btn.pack(side="left", padx=6, pady=6)
        self.speed_lbl = tk.Label(bar, text="", width=6, bg="#1d2026", fg="#c8ccd4")
        self.speed_lbl.pack(side="left")
        self.time_lbl = tk.Label(bar, text="", width=22, bg="#1d2026", fg="#c8ccd4", font=("Consolas", 10))
        self.time_lbl.pack(side="right", padx=8)
        self.slider = ttk.Scale(bar, from_=0.0, to=max(rec.duration, 1e-3), orient="horizontal",
                                command=self.on_slider)
        self.slider.pack(side="left", fill="x", expand=True, padx=6)
        self.marks = tk.Canvas(root, height=10, bg="#1d2026", highlightthickness=0)
        self.marks.pack(fill="x")

        c = self.canvas
        c.bind("<ButtonPress-1>", lambda e: self.drag_start(e, "orbit"))
        c.bind("<ButtonPress-3>", lambda e: self.drag_start(e, "pan"))
        c.bind("<B1-Motion>", self.drag_move)
        c.bind("<B3-Motion>", self.drag_move)
        c.bind("<MouseWheel>", lambda e: self.zoom(0.9 if e.delta > 0 else 1.1))
        c.bind("<Button-4>", lambda e: self.zoom(0.9))
        c.bind("<Button-5>", lambda e: self.zoom(1.1))
        c.bind("<Configure>", lambda e: self.mark_dirty())
        self.marks.bind("<Configure>", lambda e: self.draw_marks())
        root.bind("<space>", lambda e: self.toggle_play())
        root.bind("<Left>", lambda e: self.step(-1))
        root.bind("<Right>", lambda e: self.step(1))
        root.bind("<Shift-Left>", lambda e: self.seek(self.t - 1.0))
        root.bind("<Shift-Right>", lambda e: self.seek(self.t + 1.0))
        root.bind("<Home>", lambda e: self.seek(self.rec.t0))
        root.bind("<End>", lambda e: self.seek(self.rec.t1))
        for k in ("plus", "equal", "KP_Add"):
            root.bind(f"<{k}>", lambda e: self.change_speed(1))
        for k in ("minus", "KP_Subtract"):
            root.bind(f"<{k}>", lambda e: self.change_speed(-1))
        root.bind("<Key>", self.on_key)
        self.update_labels()
        self.tick()

    # --- управление ---

    def on_key(self, e):
        k = e.keysym.lower()
        if k in ("a", "ф"):
            self.show_axes = not self.show_axes
        elif k in ("l", "д"):
            self.show_labels = not self.show_labels
        elif k in ("t", "е"):
            self.show_trail = not self.show_trail
        elif k in ("g", "п"):
            self.show_grid = not self.show_grid
        elif k in ("r", "к"):
            self.reset_view()
        elif k == "1":
            self.cam.yaw, self.cam.pitch = 0.0, math.radians(15)
        elif k == "2":
            self.cam.yaw, self.cam.pitch = 0.0, math.radians(89)
        elif k == "3":
            self.cam.yaw, self.cam.pitch = math.radians(90), math.radians(5)
        else:
            return
        self.cam.update()
        self.mark_dirty()

    def reset_view(self):
        self.cam.target = list(self.rec.center())
        self.cam.yaw, self.cam.pitch, self.cam.dist = 0.0, math.radians(15), 0.7
        self.cam.update()
        self._dirty = True

    def drag_start(self, e, mode):
        self._drag = (mode, e.x, e.y)

    def drag_move(self, e):
        if not self._drag:
            return
        mode, x0, y0 = self._drag
        dx, dy = e.x - x0, e.y - y0
        self._drag = (mode, e.x, e.y)
        if mode == "orbit":
            self.cam.yaw -= dx * 0.008
            self.cam.pitch += dy * 0.008
        else:
            s = self.cam.dist * 0.0015
            for i in range(3):
                self.cam.target[i] += -dx * s * self.cam.r[i] + dy * s * self.cam.u[i]
        self.cam.update()
        self.mark_dirty()

    def zoom(self, k):
        self.cam.dist *= k
        self.cam.update()
        self.mark_dirty()

    def toggle_play(self):
        if not self.playing and self.t >= self.rec.t1:
            self.t = self.rec.t0
        self.playing = not self.playing
        self._last_wall = time.perf_counter()
        self.update_labels()

    def change_speed(self, d):
        self.speed_i = max(0, min(len(self.SPEEDS) - 1, self.speed_i + d))
        self.update_labels()

    def step(self, d):
        """Шаг на один кадр трекинга (по ближайшей руке)."""
        self.playing = False
        cands = []
        for h in (0, 1):
            ts = self.rec.times[h]
            if not ts:
                continue
            i = self.rec.index_at(h, self.t)
            j = i + 1 if d > 0 else i - (0 if i >= 0 and ts[i] < self.t - 1e-9 else 1)
            if 0 <= j < len(ts):
                cands.append(ts[j])
        if cands:
            self.seek(min(cands) if d > 0 else max(cands))
        self.update_labels()

    def seek(self, t):
        self.t = max(self.rec.t0, min(self.rec.t1, t))
        self.mark_dirty()

    def on_slider(self, v):
        if self._slider_lock:
            return
        self.t = self.rec.t0 + float(v)
        self.mark_dirty()

    def mark_dirty(self):
        self._dirty = True

    # --- цикл ---

    def tick(self):
        now = time.perf_counter()
        if self.playing:
            self.t += (now - self._last_wall) * self.SPEEDS[self.speed_i]
            if self.t >= self.rec.t1:
                self.t = self.rec.t1
                self.playing = False
                self.update_labels()
            self._dirty = True
        self._last_wall = now
        if self._dirty:
            self._dirty = False
            self.draw()
        self.root.after(16, self.tick)

    def update_labels(self):
        self.play_btn.configure(text="⏸" if self.playing else "▶")
        self.speed_lbl.configure(text=f"×{self.SPEEDS[self.speed_i]:g}")

    # --- отрисовка ---

    def draw_marks(self):
        m = self.marks
        m.delete("all")
        w = m.winfo_width()
        dur = self.rec.duration or 1.0
        for t, code, _ in self.rec.events:
            x = (t - self.rec.t0) / dur * w
            if not 0 <= x <= w:
                continue
            col = "#ff5050" if "lost" in code else "#50d070" if "acquired" in code else "#c0c0c0"
            m.create_line(x, 0, x, 10, fill=col)

    def draw(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return
        cam = self.cam
        if self.show_grid:
            self.draw_grid(w, h)

        info = []
        for hand in (0, 1):
            info.append(self.draw_hand(hand, w, h))

        # оверлей
        rel = self.t - self.rec.t0
        self._slider_lock = True
        self.slider.set(rel)
        self._slider_lock = False
        self.time_lbl.configure(text=f"{rel:9.3f} / {self.rec.duration:.3f} с")
        meta = self.rec.meta
        lines = [f"{meta.get('writer', '?')}: {os.path.basename(self.rec.path)}   сессия {meta.get('session', '')[:8]}"
                 f"   {meta.get('label') or ''}"]
        lines += [s for s in info if s]
        ev = self.last_event()
        if ev:
            lines.append(f"событие: {ev}")
        lines.append("A оси  L подписи  T след  G сетка  1/2/3 виды  R сброс")
        c.create_text(12, 10, anchor="nw", text="\n".join(lines), fill="#c8ccd4", font=("Consolas", 10))
        self.draw_marks_cursor()

    def draw_marks_cursor(self):
        m = self.marks
        m.delete("cursor")
        w = m.winfo_width()
        x = (self.t - self.rec.t0) / (self.rec.duration or 1.0) * w
        m.create_line(x, 0, x, 10, fill="white", width=2, tags="cursor")

    def last_event(self) -> str:
        i = bisect.bisect_right([e[0] for e in self.rec.events], self.t) - 1
        if i < 0:
            return ""
        t, code, detail = self.rec.events[i]
        return f"{code} {detail}  ({self.t - t:.2f} с назад)"

    def draw_grid(self, w, h):
        cam = self.cam
        cx, cz = round(cam.target[0] * 4) / 4, round(cam.target[2] * 4) / 4
        n, step = 8, 0.25
        for i in range(-n, n + 1):
            for a, b in (((cx + i * step, 0, cz - n * step), (cx + i * step, 0, cz + n * step)),
                         ((cx - n * step, 0, cz + i * step), (cx + n * step, 0, cz + i * step))):
                seg = cam.project_segment(a, b, w, h)
                if seg:
                    pa, pb = seg
                    self.canvas.create_line(pa[0], pa[1], pb[0], pb[1], fill="#262a31")
        o = cam.project((0, 0, 0), w, h)
        for axis, col in (((0.2, 0, 0), "#d04040"), ((0, 0.2, 0), "#40c040"), ((0, 0, 0.2), "#4060e0")):
            pb = cam.project(axis, w, h)
            if o and pb:
                self.canvas.create_line(o[0], o[1], pb[0], pb[1], fill=col, width=2)

    def draw_hand(self, hand: int, w: int, h: int) -> str:
        rec, cam, c = self.rec, self.cam, self.canvas
        i = rec.index_at(hand, self.t)
        name = HAND_NAMES[hand]
        if i < 0:
            return f"{name}: нет кадров"
        f = rec.frame(hand, i)
        age = self.t - rec.times[hand][i]
        stale = age > STALE_S
        col = "#5a5f68" if stale or not f.tracked else HAND_COLORS[hand]
        if f.hand_confidence != 1 and not stale and f.tracked:
            col = "#c8b44e"  # низкая уверенность
        pts = [b[:3] for b in f.bones]
        proj = [cam.project(p, w, h) for p in pts]

        if self.show_trail and not stale:
            self.draw_trail(hand, i, w, h)

        dash = (4, 3) if stale or not f.tracked else None
        for a, b in EDGES:
            if a < len(proj) and b < len(proj) and proj[a] and proj[b]:
                pa, pb = proj[a], proj[b]
                width = max(1.0, 3.5 * 0.5 / ((pa[2] + pb[2]) / 2))
                c.create_line(pa[0], pa[1], pb[0], pb[1], fill=col, width=width, dash=dash)
        for j, p in enumerate(proj):
            if not p:
                continue
            r = max(1.5, (4.0 if j in (PALM, WRIST) else 2.6) * 0.5 / p[2])
            c.create_oval(p[0] - r, p[1] - r, p[0] + r, p[1] + r, fill=col, outline="")
            if self.show_labels:
                label = P.OPENXR_JOINTS[j] if j < len(P.OPENXR_JOINTS) else str(j)
                c.create_text(p[0] + 5, p[1] - 5, anchor="sw", text=f"{j}:{label}", fill="#8a909a",
                              font=("Consolas", 7))
        if self.show_axes:
            for j, b in enumerate(f.bones):
                o = proj[j] if j < len(proj) else None
                if not o:
                    continue
                q = b[3:7]
                for v, ac in (((0.015, 0, 0), "#ff4040"), ((0, 0.015, 0), "#40ff40"), ((0, 0, 0.015), "#4080ff")):
                    d = quat_rotate(q, v)
                    pe = cam.project((b[0] + d[0], b[1] + d[1], b[2] + d[2]), w, h)
                    if pe:
                        c.create_line(o[0], o[1], pe[0], pe[1], fill=ac)

        state = "нет данных" if stale else ("трек" if f.tracked else "НЕ ОТСЛЕЖИВАЕТСЯ")
        conf = "выс" if f.hand_confidence == 1 else "низ"
        src = "" if f.source == P.SOURCE_UNEXTRAPOLATED else "  ЭКСТРАПОЛЯЦИЯ"
        wx, wy, wz = f.root[:3]
        return (f"{name}: кадр {f.frame_index:6d}  {state:<16} увер. {conf}  возраст {age * 1000:5.0f} мс  "
                f"запястье ({wx:+.3f}, {wy:+.3f}, {wz:+.3f}) м  масштаб {f.hand_scale:.3f}{src}")

    def draw_trail(self, hand: int, i: int, w: int, h: int):
        rec, cam = self.rec, self.cam
        t_end = rec.times[hand][i]
        j = rec.index_at(hand, t_end - 1.0)
        j = max(0, j)
        step = max(1, (i - j) // 50)
        tracks: dict[int, list[tuple[float, float]]] = {k: [] for k in TIPS}
        for k in range(j, i + 1, step):
            f = rec.frame(hand, k)
            if not f.tracked:
                continue
            for tip in TIPS:
                if tip < len(f.bones):
                    p = cam.project(f.bones[tip][:3], w, h)
                    if p:
                        tracks[tip].append((p[0], p[1]))
        col = "#2b4d73" if hand == 0 else "#734a24"
        for pts in tracks.values():
            if len(pts) > 1:
                self.canvas.create_line(*[v for xy in pts for v in xy], fill=col, width=1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="папка сессии или файл .qhslog")
    ap.add_argument("--selftest", action="store_true", help="загрузить, отрисовать несколько кадров и выйти")
    ap.add_argument("--at", type=float, default=0.0, help="начать с момента N с от начала записи")
    ap.add_argument("--axes", action="store_true", help="сразу показать оси суставов")
    args = ap.parse_args()

    root = tk.Tk()
    path = args.path
    if not path:
        root.withdraw()
        start = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
        path = filedialog.askopenfilename(title="Открыть запись", initialdir=start if os.path.isdir(start) else None,
                                          filetypes=[("Записи рук", "*.qhslog"), ("Все файлы", "*.*")])
        if not path:
            return
        root.deiconify()
    try:
        t = time.perf_counter()
        rec = Recording(path)
        print(f"загружено за {time.perf_counter() - t:.2f} с: левая {rec.frame_count(0)}, правая {rec.frame_count(1)} "
              f"кадров, {rec.duration:.1f} с, событий {len(rec.events)}")
    except (OSError, ValueError) as e:
        messagebox.showerror("Hand Viewer", f"Не удалось открыть {path}:\n{e}")
        sys.exit(1)

    v = Viewer(root, rec)
    v.seek(rec.t0 + args.at)
    v.show_axes = args.axes
    if args.selftest:
        root.update()
        n = 0
        t = time.perf_counter()
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            v.seek(rec.t0 + frac * rec.duration)
            for flags in ((False, False), (True, True)):
                v.show_axes, v.show_labels = flags
                v.draw()
                root.update()
                n += 1
        v.step(1), v.step(-1)
        for k in ("1", "2", "3", "r"):
            v.on_key(type("E", (), {"keysym": k})())
        v.toggle_play()
        root.after(300, root.quit)
        root.mainloop()
        print(f"selftest ok: {n} кадров за {(time.perf_counter() - t) * 1000 / n:.1f} мс/кадр, "
              f"элементов на холсте {len(v.canvas.find_all())}")
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
