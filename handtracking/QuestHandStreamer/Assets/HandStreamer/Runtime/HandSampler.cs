using System;
using UnityEngine;

namespace HandStreamer
{
    /// <summary>
    /// Чтение непредсказанных (unextrapolated) поз обеих рук через OVRPlugin и формирование HAND_FRAME.
    /// Отправляется только новый отсчёт трекинга (изменилась SampleTimeStamp).
    /// Экстраполированная поза используется только при явно включённом allowExtrapolatedFallback
    /// и помечается в каждом пакете (source = 2).
    /// </summary>
    public sealed class HandSampler
    {
        public sealed class HandStats
        {
            public string Name;
            public bool UnextrapolatedOk;
            public bool EverUnextrapolatedOk;
            public bool Tracked;
            public bool HighConfidence;
            public bool PositionsPresent;
            public int BoneCount;
            public byte LastSource;
            public uint FrameIndex;
            public long NewSamples, Duplicates, OutOfOrder, CallFailures, FallbackSamples;
            public double LastSampleTime = double.NegativeInfinity;
            public double LastCaptureToSendMs;
            public readonly RateMeter Rate = new RateMeter();

            // Последний отправленный скелет в пространстве трекинга Unity (z отражена относительно OVRPlugin)
            // — для наложения поверх рук в шлеме. Это те же данные, что уходят в запись.
            public readonly Vector3[] Joints = new Vector3[32];
            public int JointCount;
            public double LastSampleMono = double.NegativeInfinity;
        }

        public readonly HandStats[] Stats = { new HandStats { Name = "L" }, new HandStats { Name = "R" } };

        /// <summary>Вызывается при смене состояния трекинга руки: (hand, tracked).</summary>
        public Action<int, bool> TrackingChanged;

        readonly UdpLink link;
        readonly Action<byte[], int> localSink;
        readonly PacketWriter writer = new PacketWriter(Proto.MaxDatagram);
        OVRPlugin.HandState state = new OVRPlugin.HandState();

        /// <summary>
        /// Условия SDK для GetUnextrapolatedHandState (OVRPlugin.cs, Meta XR Core 209): OpenXR backend и
        /// OpenXR-скелет руки; иначе метод возвращает false и пишет предупреждение в лог на каждый вызов.
        /// </summary>
        public static bool UnextrapolatedSupported =>
            OVRPlugin.nativeXrApi == OVRPlugin.XrApi.OpenXR
            && OVRPlugin.HandSkeletonVersion == OVRHandSkeletonVersion.OpenXR;

        public static string BackendDescription =>
            $"xr_api={OVRPlugin.nativeXrApi}; skeleton={OVRPlugin.HandSkeletonVersion}";

        public HandSampler(UdpLink link, Action<byte[], int> localSink)
        {
            this.link = link;
            this.localSink = localSink;
        }

        public void Tick(ulong sessionId, bool allowExtrapolatedFallback)
        {
            Sample(0, OVRPlugin.Hand.HandLeft, sessionId, allowExtrapolatedFallback);
            Sample(1, OVRPlugin.Hand.HandRight, sessionId, allowExtrapolatedFallback);
        }

        void Sample(int h, OVRPlugin.Hand hand, ulong sessionId, bool allowFallback)
        {
            var st = Stats[h];
            byte source = Proto.SourceUnextrapolated;
            bool ok = UnextrapolatedSupported && OVRPlugin.GetUnextrapolatedHandState(hand, ref state);
            st.UnextrapolatedOk = ok;
            if (ok) st.EverUnextrapolatedOk = true;
            if (!ok)
            {
                st.CallFailures++;
                if (!allowFallback) { SetTracked(h, st, false); return; }
                if (!OVRPlugin.GetHandState(OVRPlugin.Step.Render, hand, ref state)) { SetTracked(h, st, false); return; }
                source = Proto.SourceExtrapolated;
            }

            double sampleTime = state.SampleTimeStamp;
            if (sampleTime == st.LastSampleTime)
            {
                st.Duplicates++;
                return;
            }
            if (sampleTime < st.LastSampleTime) st.OutOfOrder++;
            st.LastSampleTime = sampleTime;
            st.FrameIndex++;
            st.NewSamples++;
            if (source == Proto.SourceExtrapolated) st.FallbackSamples++;
            st.LastSource = source;

            double ovrNow = OVRPlugin.GetTimeInSeconds();
            double monoNow = MonoClock.Seconds;
            st.Rate.Tick(monoNow);
            st.LastCaptureToSendMs = (ovrNow - sampleTime) * 1000.0;

            bool tracked = (state.Status & OVRPlugin.HandStatus.HandTracked) != 0;
            SetTracked(h, st, tracked);
            st.HighConfidence = state.HandConfidence == OVRPlugin.TrackingConfidence.High;

            var rotations = state.BoneRotations;
            var positions = state.BonePositions;

            int maxBones = (Proto.MaxDatagram - Proto.HandFrameFixedSize) / Proto.BoneSize;
            int boneCount = Math.Max(rotations?.Length ?? 0, positions?.Length ?? 0);
            if (boneCount > maxBones) boneCount = maxBones;
            st.BoneCount = boneCount;
            st.PositionsPresent = positions != null && positions.Length > 0;

            byte flags = 0;
            if (tracked) flags |= Proto.FlagTracked;
            if ((state.Status & OVRPlugin.HandStatus.InputStateValid) != 0) flags |= Proto.FlagInputValid;
            if (st.PositionsPresent) flags |= Proto.FlagPositions;
            if (rotations != null && rotations.Length > 0) flags |= Proto.FlagRotations;
            if ((state.Status & OVRPlugin.HandStatus.SystemGestureInProgress) != 0) flags |= Proto.FlagSystemGesture;

            int jc = 0;
            if (positions != null)
            {
                jc = Math.Min(positions.Length, st.Joints.Length);
                for (int i = 0; i < jc; i++)
                    st.Joints[i] = new Vector3(positions[i].x, positions[i].y, -positions[i].z);
            }
            st.JointCount = jc;
            st.LastSampleMono = monoNow;

            var w = writer;
            w.Header(Proto.MsgHandFrame);
            w.U64(sessionId);
            w.U32(st.FrameIndex);
            w.U8((byte)h);
            w.U8(source);
            w.U8(st.HighConfidence ? Proto.ConfHigh : Proto.ConfLow);
            w.U8((byte)boneCount);
            var fc = state.FingerConfidences;
            for (int i = 0; i < 5; i++)
            {
                if (fc != null && i < fc.Length)
                    w.U8(fc[i] == OVRPlugin.TrackingConfidence.High ? Proto.ConfHigh : Proto.ConfLow);
                else
                    w.U8(Proto.ConfUnknown);
            }
            w.U8(flags);
            w.U16(0);
            w.U32((uint)state.Status);
            w.F64(sampleTime);
            w.F64(state.RequestedTimeStamp);
            w.F64(ovrNow);
            w.F64(monoNow);
            w.U32((uint)Time.frameCount);
            w.F32(state.HandScale);
            WritePose(w, state.RootPose);
            for (int i = 0; i < boneCount; i++)
            {
                if (positions != null && i < positions.Length)
                {
                    w.F32(positions[i].x); w.F32(positions[i].y); w.F32(positions[i].z);
                }
                else
                {
                    w.F32(0); w.F32(0); w.F32(0);
                }
                if (rotations != null && i < rotations.Length)
                {
                    w.F32(rotations[i].x); w.F32(rotations[i].y); w.F32(rotations[i].z); w.F32(rotations[i].w);
                }
                else
                {
                    w.F32(0); w.F32(0); w.F32(0); w.F32(1);
                }
            }

            // Локальная запись получает пакет даже без связи с ПК.
            bool sent = link.Send(w.Buf, w.Pos);
            if (!sent) localSink?.Invoke(w.Buf, w.Pos);
        }

        static void WritePose(PacketWriter w, OVRPlugin.Posef p)
        {
            w.F32(p.Position.x); w.F32(p.Position.y); w.F32(p.Position.z);
            w.F32(p.Orientation.x); w.F32(p.Orientation.y); w.F32(p.Orientation.z); w.F32(p.Orientation.w);
        }

        void SetTracked(int h, HandStats st, bool tracked)
        {
            if (st.Tracked == tracked) return;
            st.Tracked = tracked;
            TrackingChanged?.Invoke(h, tracked);
        }
    }

    /// <summary>Частота событий в скользящем окне 1 с.</summary>
    public sealed class RateMeter
    {
        readonly double[] times = new double[512];
        int head, count;

        public void Tick(double t)
        {
            times[head] = t;
            head = (head + 1) % times.Length;
            if (count < times.Length) count++;
        }

        public float Hz(double now)
        {
            int n = 0;
            for (int i = 0; i < count; i++)
            {
                int idx = (head - 1 - i + times.Length) % times.Length;
                if (now - times[idx] <= 1.0) n++;
                else break;
            }
            return n;
        }
    }
}
