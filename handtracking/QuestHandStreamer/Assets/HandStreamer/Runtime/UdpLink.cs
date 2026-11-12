using System;
using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Threading;

namespace HandStreamer
{
    /// <summary>
    /// UDP-канал Quest ↔ ПК. Фоновый поток принимает пакеты и сразу отвечает на SYNC_REQ
    /// (метки t2/t3 в домене mono без задержки на кадр Unity). Остальные входящие сообщения
    /// передаются в главный поток через очередь <see cref="Inbox"/>.
    /// </summary>
    public sealed class UdpLink : IDisposable
    {
        public struct Incoming
        {
            public byte[] Data;
            public int Length;
            public IPEndPoint From;
            public double MonoTime;
        }

        public readonly ConcurrentQueue<Incoming> Inbox = new ConcurrentQueue<Incoming>();

        /// <summary>Вызывается для каждого отправленного (dir=1) и принятого (dir=0) пакета; может вызываться из фонового потока.</summary>
        public Action<byte, long, byte[], int> PacketLogged;

        public readonly int LocalPort;
        public string LastError { get; private set; } = "";

        long packetsSent, bytesSent, sendErrors, packetsReceived, notConnectedDrops;
        public long PacketsSent => Interlocked.Read(ref packetsSent);
        public long BytesSent => Interlocked.Read(ref bytesSent);
        public long SendErrors => Interlocked.Read(ref sendErrors);
        public long PacketsReceived => Interlocked.Read(ref packetsReceived);
        public long NotConnectedDrops => Interlocked.Read(ref notConnectedDrops);

        /// <summary>Время (mono) последнего SYNC_REQ от ПК — признак живой связи.</summary>
        public double LastSyncRequestMono => Volatile.Read(ref lastSyncRequestMono);
        double lastSyncRequestMono = double.NegativeInfinity;

        readonly Socket socket;
        readonly Thread rxThread;
        volatile bool running = true;
        volatile IPEndPoint target;
        int seq;
        readonly PacketWriter syncWriter = new PacketWriter(64);

        public IPEndPoint Target => target;

        public UdpLink(int localPort)
        {
            LocalPort = localPort;
            socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
            socket.EnableBroadcast = true;
            socket.ReceiveBufferSize = 1 << 18;
            socket.SendBufferSize = 1 << 18;
            socket.Bind(new IPEndPoint(IPAddress.Any, localPort));
            rxThread = new Thread(ReceiveLoop) { IsBackground = true, Name = "HandStreamer UDP RX" };
            rxThread.Start();
        }

        public void SetTarget(IPEndPoint ep) { target = ep; }

        /// <summary>Отправка на текущий адрес ПК. Проставляет сквозной seq в заголовок.</summary>
        public bool Send(byte[] buf, int len)
        {
            var ep = target;
            if (ep == null)
            {
                Interlocked.Increment(ref notConnectedDrops);
                return false;
            }
            return SendTo(buf, len, ep);
        }

        bool SendTo(byte[] buf, int len, IPEndPoint ep)
        {
            uint s = (uint)Interlocked.Increment(ref seq);
            PacketWriter.PatchU32(buf, 8, s);
            long t = MonoClock.Nanoseconds;
            try
            {
                socket.SendTo(buf, 0, len, SocketFlags.None, ep);
                Interlocked.Increment(ref packetsSent);
                Interlocked.Add(ref bytesSent, len);
            }
            catch (Exception e)
            {
                Interlocked.Increment(ref sendErrors);
                LastError = e.Message;
                return false;
            }
            PacketLogged?.Invoke(1, t, buf, len);
            return true;
        }

        void ReceiveLoop()
        {
            var buf = new byte[2048];
            EndPoint any = new IPEndPoint(IPAddress.Any, 0);
            while (running)
            {
                int n;
                try
                {
                    n = socket.ReceiveFrom(buf, ref any);
                }
                catch (SocketException e)
                {
                    if (!running) break;
                    // На Windows/Android ICMP "port unreachable" может прерывать ReceiveFrom — продолжаем.
                    LastError = e.Message;
                    continue;
                }
                catch (ObjectDisposedException)
                {
                    break;
                }

                double t2 = MonoClock.Seconds;
                if (!Proto.HasValidHeader(buf, n)) continue;
                Interlocked.Increment(ref packetsReceived);
                var from = (IPEndPoint)any;
                PacketLogged?.Invoke(0, (long)(t2 * 1e9), buf, n);

                byte type = buf[4];
                if (type == Proto.MsgSyncReq && n >= Proto.HeaderSize + 12)
                {
                    var r = new PacketReader(buf, Proto.HeaderSize);
                    uint syncId = r.U32();
                    long pcT1 = r.I64();
                    Volatile.Write(ref lastSyncRequestMono, t2);
                    lock (syncWriter)
                    {
                        syncWriter.Header(Proto.MsgSyncResp);
                        syncWriter.U32(syncId);
                        syncWriter.I64(pcT1);
                        syncWriter.F64(t2);
                        int t3Pos = syncWriter.Pos;
                        syncWriter.F64(0);
                        // t3 записываем непосредственно перед отправкой
                        double t3 = MonoClock.Seconds;
                        int end = syncWriter.Pos;
                        syncWriter.Pos = t3Pos;
                        syncWriter.F64(t3);
                        syncWriter.Pos = end;
                        SendTo(syncWriter.Buf, end, from);
                    }
                    continue;
                }

                var copy = new byte[n];
                Buffer.BlockCopy(buf, 0, copy, 0, n);
                Inbox.Enqueue(new Incoming { Data = copy, Length = n, From = from, MonoTime = t2 });
            }
        }

        public void Dispose()
        {
            running = false;
            try { socket.Close(); } catch { /* ignore */ }
            rxThread.Join(500);
        }
    }
}
