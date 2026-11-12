"""Разбор записи .qhslog: CSV кадров рук, синхронизация, события и отчёт о качестве.

Использование:
    python convert.py recordings/<сессия>            (берёт pc.qhslog в папке)
    python convert.py путь/к/файлу.qhslog [--out папка]

Результат в папке рядом с журналом:
    hands.csv   — один ряд на новый отсчёт руки; время в доменах ovr и ПК
    sync.csv    — все обмены синхронизации (t1..t4, RTT, смещение)
    events.jsonl, status.jsonl
    report.json — частоты, потери, повторы, модель часов, задержки, проверка временных доменов
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys

from qhs import protocol as P
from qhs.clocksync import SyncSample, fit, fit_ovr_mono
from qhs.logfile import DIR_RECEIVED, read_log


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(xs: list[float], scale: float = 1.0) -> dict:
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": min(xs) * scale,
        "p05": percentile(xs, 0.05) * scale,
        "median": statistics.median(xs) * scale,
        "p95": percentile(xs, 0.95) * scale,
        "max": max(xs) * scale,
    }


def convert(log_path: str, out_dir: str) -> dict:
    meta, records = read_log(log_path)
    writer_side = meta.get("writer", "pc")
    os.makedirs(out_dir, exist_ok=True)

    frames: list[tuple[int, P.HandFrame]] = []  # (t_ns, frame)
    syncs: list[SyncSample] = []
    events, statuses = [], []
    seqs: list[int] = []
    parse_errors = 0

    for rec in records:
        if rec.direction != DIR_RECEIVED and writer_side == "pc":
            continue
        try:
            hdr = P.parse_header(rec.data)
        except P.ProtocolError:
            parse_errors += 1
            continue
        if writer_side == "pc" and hdr.type in (P.MSG_SYNC_REQ, P.MSG_COMMAND, P.MSG_BEACON):
            continue
        if writer_side == "quest" and rec.direction != 1:
            continue  # на Quest интересны отправленные пакеты
        seqs.append(hdr.seq)
        try:
            if hdr.type == P.MSG_HAND_FRAME:
                frames.append((rec.t_ns, P.parse_hand_frame(rec.data, hdr.seq)))
            elif hdr.type == P.MSG_SYNC_RESP and writer_side == "pc":
                r = P.parse_sync_resp(rec.data, hdr.seq)
                syncs.append(SyncSample(r.pc_t1_ns / 1e9, r.q_t2, r.q_t3, rec.t_ns / 1e9))
            elif hdr.type == P.MSG_EVENT:
                events.append({"t_ns": rec.t_ns, **P.parse_json(rec.data)})
            elif hdr.type == P.MSG_STATUS:
                statuses.append({"t_ns": rec.t_ns, **P.parse_json(rec.data)})
        except (P.ProtocolError, ValueError) as e:
            parse_errors += 1
            print(f"пакет seq={hdr.seq}: {e}", file=sys.stderr)

    # --- часы ---
    model = fit(syncs) if writer_side == "pc" else None
    rel = fit_ovr_mono([(f.ovr_now, f.mono_now) for _, f in frames])

    def sample_to_pc(f: P.HandFrame) -> float:
        if model is None or rel is None:
            return float("nan")
        return model.mono_to_pc(rel.ovr_to_mono(f.sample_time))

    # --- CSV кадров ---
    max_bones = max((f.bone_count for _, f in frames), default=0)
    joint_names = [P.OPENXR_JOINTS[i] if i < len(P.OPENXR_JOINTS) and max_bones == 26 else f"j{i:02d}"
                   for i in range(max_bones)]
    cols = ["seq", "session", "hand", "frame_index", "source", "tracked", "input_valid", "hand_conf",
            "finger_conf", "status", "sample_time_ovr_s", "requested_time_ovr_s", "ovr_now_s", "mono_now_s",
            "sample_time_pc_s", "recv_time_pc_s", "capture_to_recv_ms", "capture_to_send_ms", "unity_frame",
            "hand_scale", "root_px", "root_py", "root_pz", "root_qx", "root_qy", "root_qz", "root_qw"]
    for n in joint_names:
        cols += [f"{n}_{c}" for c in ("px", "py", "pz", "qx", "qy", "qz", "qw")]

    per_hand = {0: [], 1: []}
    capture_to_recv, capture_to_send = [], []
    with open(os.path.join(out_dir, "hands.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for t_ns, f in frames:
            spc = sample_to_pc(f)
            recv = t_ns / 1e9 if writer_side == "pc" else float("nan")
            c2r = (recv - spc) * 1e3
            c2s = (f.ovr_now - f.sample_time) * 1e3
            if math.isfinite(c2r):
                capture_to_recv.append(c2r / 1e3)
            capture_to_send.append(c2s / 1e3)
            per_hand[f.hand].append(f)
            row = [f.seq, f"{f.session_id:016x}", f.hand_name, f.frame_index, f.source, int(f.tracked),
                   int(bool(f.flags & P.FLAG_INPUT_VALID)), f.hand_confidence,
                   "".join(str(c) if c < 2 else "?" for c in f.finger_confidence), f.status,
                   repr(f.sample_time), repr(f.requested_time), repr(f.ovr_now), repr(f.mono_now),
                   repr(spc), repr(recv), f"{c2r:.3f}", f"{c2s:.3f}", f.unity_frame, f.hand_scale, *f.root]
            for i in range(max_bones):
                row += list(f.bones[i]) if i < len(f.bones) else [""] * 7
            w.writerow(row)

    with open(os.path.join(out_dir, "sync.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t1_pc_s", "t2_quest_mono_s", "t3_quest_mono_s", "t4_pc_s", "rtt_ms", "offset_mono_minus_pc_ms"])
        for s in syncs:
            w.writerow([repr(s.t1), repr(s.t2), repr(s.t3), repr(s.t4), f"{s.rtt * 1e3:.4f}", f"{s.offset * 1e3:.4f}"])

    for name, items in (("events.jsonl", events), ("status.jsonl", statuses)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            for e in items:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    # --- отчёт ---
    seq_lost, seq_reordered, seq_dup = seq_stats(seqs)
    hands_report = {}
    for h, fs in per_hand.items():
        if not fs:
            continue
        ts = [f.sample_time for f in fs]
        dts = [b - a for a, b in zip(ts, ts[1:])]
        span = ts[-1] - ts[0]
        idx = [f.frame_index for f in fs]
        idx_gaps = sum(max(0, b - a - 1) for a, b in zip(idx, idx[1:]))
        hands_report["left" if h == 0 else "right"] = {
            "frames": len(fs),
            "tracked_fraction": sum(f.tracked for f in fs) / len(fs),
            "high_confidence_fraction": sum(f.hand_confidence == 1 for f in fs) / len(fs),
            "sources": {str(k): sum(f.source == k for f in fs) for k in sorted({f.source for f in fs})},
            "bone_count": sorted({f.bone_count for f in fs}),
            "positions_present": all(f.flags & P.FLAG_POSITIONS for f in fs),
            "span_s": span,
            "mean_rate_hz": (len(fs) - 1) / span if span > 0 else float("nan"),
            "sample_interval_ms": summary(dts, 1e3),
            "non_increasing_sample_time": sum(d <= 0 for d in dts),
            "intervals_over_1s": sum(d > 1.0 for d in dts),
            "frame_index_gaps": idx_gaps,
        }

    domain_check = {
        "capture_to_send_ms": summary(capture_to_send, 1e3),
        "negative_capture_to_send": sum(x < 0 for x in capture_to_send),
        "comment": "SampleTimeStamp считается в домене GetTimeInSeconds, если capture_to_send >= 0 и порядка десятков мс",
    }
    domain_check["consistent"] = bool(capture_to_send) and domain_check["negative_capture_to_send"] == 0 \
        and percentile(capture_to_send, 0.95) < 0.5

    report = {
        "source_log": os.path.abspath(log_path),
        "writer": writer_side,
        "metadata": meta,
        "packets": {"total": len(seqs), "lost_by_seq": seq_lost, "reordered": seq_reordered,
                    "duplicated": seq_dup, "parse_errors": parse_errors},
        "hands": hands_report,
        "clock_mono_vs_pc": model.to_dict() if model else None,
        "clock_ovr_vs_mono": rel.to_dict() if rel else None,
        "timestamp_domain_check": domain_check,
        "capture_to_pc_receive_ms": summary(capture_to_recv, 1e3),
        "events": [e.get("code") for e in events],
    }
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, allow_nan=True)
    return report


def seq_stats(seqs: list[int]) -> tuple[int, int, int]:
    """Потери, перестановки, дубликаты по сквозному seq отправителя."""
    if not seqs:
        return 0, 0, 0
    seen = set()
    dup = 0
    reordered = 0
    max_seq = seqs[0] - 1
    for s in seqs:
        if s in seen:
            dup += 1
            continue
        seen.add(s)
        if s < max_seq:
            reordered += 1
        max_seq = max(max_seq, s)
    lost = (max(seen) - min(seen) + 1) - len(seen)
    return lost, reordered, dup


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="папка сессии или файл .qhslog")
    ap.add_argument("--out", help="папка для результатов (по умолчанию — рядом с журналом)")
    args = ap.parse_args()
    path = args.path
    if os.path.isdir(path):
        path = os.path.join(path, "pc.qhslog")
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(path)),
                                   os.path.splitext(os.path.basename(path))[0] + "_converted")
    r = convert(path, out)
    print(f"Готово: {out}")
    print(json.dumps({k: r[k] for k in ("packets", "hands", "clock_mono_vs_pc", "capture_to_pc_receive_ms")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
