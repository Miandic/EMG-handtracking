"""Протокол QHS1: разбор и формирование пакетов. Спецификация: docs/protocol.md."""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field

MAGIC = b"QHS1"
VERSION = 1
HEADER = struct.Struct("<4sBBHI")  # magic, type, version, reserved, seq
HEADER_SIZE = HEADER.size  # 12
MAX_DATAGRAM = 1200

PC_PORT = 47000
QUEST_PORT = 47001

MSG_HAND_FRAME = 1
MSG_STATUS = 2
MSG_SYNC_REQ = 3
MSG_SYNC_RESP = 4
MSG_BEACON = 5
MSG_COMMAND = 6
MSG_EVENT = 7

MSG_NAMES = {
    MSG_HAND_FRAME: "hand_frame",
    MSG_STATUS: "status",
    MSG_SYNC_REQ: "sync_req",
    MSG_SYNC_RESP: "sync_resp",
    MSG_BEACON: "beacon",
    MSG_COMMAND: "command",
    MSG_EVENT: "event",
}

SOURCE_UNEXTRAPOLATED = 1
SOURCE_EXTRAPOLATED = 2

FLAG_TRACKED = 1 << 0
FLAG_INPUT_VALID = 1 << 1
FLAG_POSITIONS = 1 << 2
FLAG_ROTATIONS = 1 << 3
FLAG_SYSTEM_GESTURE = 1 << 4

# Тело HAND_FRAME до массива суставов (смещение 12..107)
HAND_FIXED = struct.Struct("<QIBBBB5sBHIddddIf7f")
HAND_FIXED_SIZE = HEADER_SIZE + HAND_FIXED.size  # 108
BONE = struct.Struct("<7f")
SYNC_REQ = struct.Struct("<Iq")
SYNC_RESP = struct.Struct("<Iqdd")

assert HAND_FIXED_SIZE == 108

# Имена 26 суставов OpenXR (XR_EXT_hand_tracking, порядок XrHandJointEXT).
# Соответствие индексов OVRPlugin этому порядку требует проверки на устройстве.
OPENXR_JOINTS = [
    "palm", "wrist",
    "thumb_metacarpal", "thumb_proximal", "thumb_distal", "thumb_tip",
    "index_metacarpal", "index_proximal", "index_intermediate", "index_distal", "index_tip",
    "middle_metacarpal", "middle_proximal", "middle_intermediate", "middle_distal", "middle_tip",
    "ring_metacarpal", "ring_proximal", "ring_intermediate", "ring_distal", "ring_tip",
    "little_metacarpal", "little_proximal", "little_intermediate", "little_distal", "little_tip",
]


class ProtocolError(ValueError):
    pass


@dataclass
class Header:
    type: int
    version: int
    seq: int


@dataclass
class HandFrame:
    seq: int
    session_id: int
    frame_index: int
    hand: int
    source: int
    hand_confidence: int
    bone_count: int
    finger_confidence: tuple[int, ...]
    flags: int
    status: int
    sample_time: float
    requested_time: float
    ovr_now: float
    mono_now: float
    unity_frame: int
    hand_scale: float
    root: tuple[float, ...]  # px py pz qx qy qz qw
    bones: list[tuple[float, ...]] = field(default_factory=list)

    @property
    def tracked(self) -> bool:
        return bool(self.flags & FLAG_TRACKED)

    @property
    def hand_name(self) -> str:
        return "left" if self.hand == 0 else "right"


@dataclass
class SyncResp:
    seq: int
    sync_id: int
    pc_t1_ns: int
    q_t2: float
    q_t3: float


def parse_header(data: bytes) -> Header:
    if len(data) < HEADER_SIZE:
        raise ProtocolError("short packet")
    magic, mtype, ver, _res, seq = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic {magic!r}")
    if ver != VERSION:
        raise ProtocolError(f"unsupported version {ver}")
    return Header(mtype, ver, seq)


def parse_hand_frame(data: bytes, seq: int) -> HandFrame:
    if len(data) < HAND_FIXED_SIZE:
        raise ProtocolError("short hand frame")
    v = HAND_FIXED.unpack_from(data, HEADER_SIZE)
    (session_id, frame_index, hand, source, conf, bone_count, finger, flags, _res, status,
     sample_time, requested_time, ovr_now, mono_now, unity_frame, hand_scale) = v[:16]
    root = tuple(v[16:23])
    expected = HAND_FIXED_SIZE + bone_count * BONE.size
    if len(data) < expected:
        raise ProtocolError(f"hand frame truncated: {len(data)} < {expected}")
    bones = [BONE.unpack_from(data, HAND_FIXED_SIZE + i * BONE.size) for i in range(bone_count)]
    return HandFrame(seq, session_id, frame_index, hand, source, conf, bone_count, tuple(finger), flags, status,
                     sample_time, requested_time, ovr_now, mono_now, unity_frame, hand_scale, root, bones)


def parse_sync_resp(data: bytes, seq: int) -> SyncResp:
    sync_id, t1, t2, t3 = SYNC_RESP.unpack_from(data, HEADER_SIZE)
    return SyncResp(seq, sync_id, t1, t2, t3)


def parse_json(data: bytes) -> dict:
    return json.loads(data[HEADER_SIZE:].decode("utf-8"))


def build(msg_type: int, seq: int, body: bytes) -> bytes:
    return HEADER.pack(MAGIC, msg_type, VERSION, 0, seq & 0xFFFFFFFF) + body


def build_json(msg_type: int, seq: int, obj: dict) -> bytes:
    pkt = build(msg_type, seq, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if len(pkt) > MAX_DATAGRAM:
        raise ProtocolError("json message too large")
    return pkt


def build_sync_req(seq: int, sync_id: int, pc_t1_ns: int) -> bytes:
    return build(MSG_SYNC_REQ, seq, SYNC_REQ.pack(sync_id & 0xFFFFFFFF, pc_t1_ns))


def build_sync_resp(seq: int, sync_id: int, pc_t1_ns: int, q_t2: float, q_t3: float) -> bytes:
    return build(MSG_SYNC_RESP, seq, SYNC_RESP.pack(sync_id, pc_t1_ns, q_t2, q_t3))


def build_hand_frame(seq: int, f: HandFrame) -> bytes:
    body = HAND_FIXED.pack(f.session_id, f.frame_index, f.hand, f.source, f.hand_confidence, len(f.bones),
                           bytes(f.finger_confidence), f.flags, 0, f.status, f.sample_time, f.requested_time,
                           f.ovr_now, f.mono_now, f.unity_frame, f.hand_scale, *f.root)
    body += b"".join(BONE.pack(*b) for b in f.bones)
    return build(MSG_HAND_FRAME, seq, body)
