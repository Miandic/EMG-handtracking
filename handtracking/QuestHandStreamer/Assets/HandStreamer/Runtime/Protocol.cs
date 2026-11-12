using System;
using System.Diagnostics;
using System.Text;

namespace HandStreamer
{
    /// <summary>Константы протокола QHS1. Спецификация: docs/protocol.md.</summary>
    public static class Proto
    {
        public const byte Version = 1;
        public const int HeaderSize = 12;
        public const int MaxDatagram = 1200;
        public const int HandFrameFixedSize = 108;
        public const int BoneSize = 28; // 7 x f32

        public const byte MsgHandFrame = 1;
        public const byte MsgStatus = 2;
        public const byte MsgSyncReq = 3;
        public const byte MsgSyncResp = 4;
        public const byte MsgBeacon = 5;
        public const byte MsgCommand = 6;
        public const byte MsgEvent = 7;

        public const int DefaultPcPort = 47000;
        public const int DefaultQuestPort = 47001;

        public const byte SourceUnextrapolated = 1;
        public const byte SourceExtrapolated = 2;

        public const byte FlagTracked = 1 << 0;
        public const byte FlagInputValid = 1 << 1;
        public const byte FlagPositions = 1 << 2;
        public const byte FlagRotations = 1 << 3;
        public const byte FlagSystemGesture = 1 << 4;

        public const byte ConfLow = 0, ConfHigh = 1, ConfUnknown = 255;

        public static readonly byte[] MagicBytes = { (byte)'Q', (byte)'H', (byte)'S', (byte)'1' };

        public static bool HasValidHeader(byte[] buf, int len)
        {
            return len >= HeaderSize && buf[0] == MagicBytes[0] && buf[1] == MagicBytes[1]
                   && buf[2] == MagicBytes[2] && buf[3] == MagicBytes[3] && buf[5] == Version;
        }
    }

    /// <summary>Монотонные часы Quest (домен "mono"): Stopwatch, секунды.</summary>
    public static class MonoClock
    {
        static readonly double InvFreq = 1.0 / Stopwatch.Frequency;
        public static double Seconds => Stopwatch.GetTimestamp() * InvFreq;
        public static long Nanoseconds => (long)(Stopwatch.GetTimestamp() * (1e9 * InvFreq));
    }

    /// <summary>Запись little-endian в заранее выделенный буфер (без аллокаций).</summary>
    public sealed class PacketWriter
    {
        public readonly byte[] Buf;
        public int Pos;

        public PacketWriter(int capacity) { Buf = new byte[capacity]; }

        public void Reset() { Pos = 0; }

        public void Header(byte type)
        {
            Pos = 0;
            Bytes(Proto.MagicBytes);
            U8(type);
            U8(Proto.Version);
            U16(0);
            U32(0); // seq проставляет UdpLink в момент отправки
        }

        public void U8(byte v) { Buf[Pos++] = v; }
        public void U16(ushort v) { Buf[Pos++] = (byte)v; Buf[Pos++] = (byte)(v >> 8); }

        public void U32(uint v)
        {
            Buf[Pos++] = (byte)v; Buf[Pos++] = (byte)(v >> 8);
            Buf[Pos++] = (byte)(v >> 16); Buf[Pos++] = (byte)(v >> 24);
        }

        public void U64(ulong v) { U32((uint)v); U32((uint)(v >> 32)); }
        public void I64(long v) { U64((ulong)v); }
        public void F32(float v) { U32((uint)BitConverter.SingleToInt32Bits(v)); }
        public void F64(double v) { U64((ulong)BitConverter.DoubleToInt64Bits(v)); }

        public void Bytes(byte[] src) { Buffer.BlockCopy(src, 0, Buf, Pos, src.Length); Pos += src.Length; }

        public void Json(string json)
        {
            int n = Encoding.UTF8.GetBytes(json, 0, json.Length, Buf, Pos);
            Pos += n;
        }

        public static void PatchU32(byte[] buf, int offset, uint v)
        {
            buf[offset] = (byte)v; buf[offset + 1] = (byte)(v >> 8);
            buf[offset + 2] = (byte)(v >> 16); buf[offset + 3] = (byte)(v >> 24);
        }
    }

    /// <summary>Чтение little-endian из полученного пакета.</summary>
    public struct PacketReader
    {
        readonly byte[] buf;
        public int Pos;

        public PacketReader(byte[] buf, int pos) { this.buf = buf; Pos = pos; }

        public uint U32()
        {
            uint v = (uint)(buf[Pos] | (buf[Pos + 1] << 8) | (buf[Pos + 2] << 16) | (buf[Pos + 3] << 24));
            Pos += 4;
            return v;
        }

        public long I64()
        {
            ulong lo = U32();
            ulong hi = U32();
            return (long)(lo | (hi << 32));
        }
    }
}
