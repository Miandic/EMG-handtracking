using System;
using System.Collections.Concurrent;
using System.IO;
using System.Text;
using System.Threading;

namespace HandStreamer
{
    /// <summary>
    /// Локальная резервная запись на Quest в формате .qhslog (см. docs/protocol.md).
    /// Запись на диск — в отдельном потоке, главный поток только ставит пакеты в очередь.
    /// </summary>
    public sealed class LogWriter : IDisposable
    {
        public readonly string FilePath;
        long bytesWritten, dropped;
        public long BytesWritten => Interlocked.Read(ref bytesWritten);
        public long Dropped => Interlocked.Read(ref dropped);
        public string Error { get; private set; }

        readonly BlockingCollection<byte[]> queue = new BlockingCollection<byte[]>(new ConcurrentQueue<byte[]>(), 50000);
        readonly Thread thread;
        readonly FileStream stream;

        public LogWriter(string path, string metadataJson)
        {
            FilePath = path;
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read, 1 << 16);
            byte[] magic = Encoding.ASCII.GetBytes("QHSLOG01");
            byte[] meta = Encoding.UTF8.GetBytes(metadataJson);
            stream.Write(magic, 0, magic.Length);
            stream.Write(BitConverter.GetBytes((uint)meta.Length), 0, 4);
            stream.Write(meta, 0, meta.Length);
            stream.Flush();
            bytesWritten = magic.Length + 4 + meta.Length;
            thread = new Thread(Loop) { IsBackground = true, Name = "HandStreamer LogWriter" };
            thread.Start();
        }

        /// <summary>Потокобезопасно. dir: 0 — принят, 1 — отправлен.</summary>
        public void Write(byte dir, long timeNs, byte[] data, int len)
        {
            var rec = new byte[11 + len];
            rec[0] = dir;
            ulong t = (ulong)timeNs;
            for (int i = 0; i < 8; i++) rec[1 + i] = (byte)(t >> (8 * i));
            rec[9] = (byte)len;
            rec[10] = (byte)(len >> 8);
            Buffer.BlockCopy(data, 0, rec, 11, len);
            try
            {
                if (!queue.TryAdd(rec)) Interlocked.Increment(ref dropped);
            }
            catch (InvalidOperationException)
            {
                // запись уже закрыта
                Interlocked.Increment(ref dropped);
            }
        }

        void Loop()
        {
            double lastFlush = MonoClock.Seconds;
            try
            {
                while (!queue.IsCompleted)
                {
                    if (queue.TryTake(out var rec, 200))
                    {
                        stream.Write(rec, 0, rec.Length);
                        Interlocked.Add(ref bytesWritten, rec.Length);
                    }
                    double now = MonoClock.Seconds;
                    if (now - lastFlush > 1.0)
                    {
                        stream.Flush();
                        lastFlush = now;
                    }
                }
            }
            catch (Exception e)
            {
                Error = e.Message;
            }
        }

        public void Dispose()
        {
            queue.CompleteAdding();
            thread.Join(3000);
            try
            {
                stream.Flush(true);
                stream.Dispose();
            }
            catch (Exception e)
            {
                Error = e.Message;
            }
        }
    }
}
