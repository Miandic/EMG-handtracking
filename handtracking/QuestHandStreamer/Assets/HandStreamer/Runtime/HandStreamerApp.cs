using System;
using System.IO;
using System.Net;
using System.Text;
using UnityEngine;

namespace HandStreamer
{
    /// <summary>
    /// Главный компонент: связь с ПК, опрос рук, управление записью, статус.
    /// Сцена: OVRCameraRig (passthrough, hand tracking) + объект с этим компонентом.
    /// </summary>
    public sealed class HandStreamerApp : MonoBehaviour
    {
        public const string AppVersion = "0.1.0";

        public StreamerConfig Config { get; private set; }
        public UdpLink Link { get; private set; }
        public HandSampler Sampler { get; private set; }
        public HandSkeletonOverlay SkeletonOverlay { get; private set; }
        public ulong SessionId { get; private set; }
        public bool Recording => SessionId != 0;
        public double RecordingStartMono { get; private set; }
        public LogWriter Recorder => recorder;
        public string PcName { get; private set; } = "";
        public bool AutoDiscovery => string.IsNullOrEmpty(Config.pcIp);
        public string StartupError { get; private set; } = "";
        public float DisplayHz { get; private set; }

        public bool PcConnected => Link != null && MonoClock.Seconds - Link.LastSyncRequestMono < 2.5;

        volatile LogWriter recorder;
        readonly PacketWriter jsonWriter = new PacketWriter(Proto.MaxDatagram);
        double nextStatusTime;
        double lastBeaconMono = double.NegativeInfinity;
        bool lastConnected;
        bool reportedUnextrapolatedFailure;
        string sessionLabel = "";

        void Start()
        {
            Screen.sleepTimeout = SleepTimeout.NeverSleep;
            Application.runInBackground = true;
            Config = StreamerConfig.Load();
            ApplyDisplayFrequency();

            try
            {
                Link = new UdpLink(Config.localPort);
                Link.PacketLogged = (dir, t, buf, len) => recorder?.Write(dir, t, buf, len);
                ApplyManualTarget();
            }
            catch (Exception e)
            {
                StartupError = "UDP: " + e.Message;
                Debug.LogError("[HandStreamer] " + StartupError);
            }

            Sampler = new HandSampler(Link, (buf, len) => recorder?.Write(1, MonoClock.Nanoseconds, buf, len));
            Sampler.TrackingChanged = (h, tracked) =>
                SendEvent(tracked ? "tracking_acquired" : "tracking_lost", h == 0 ? "left" : "right");


            SkeletonOverlay = gameObject.AddComponent<HandSkeletonOverlay>();
            SkeletonOverlay.Init(Sampler, Config.showSkeletonOverlay);
            gameObject.AddComponent<HandStreamerHud>().Init(this);
            SendEvent("app_start", $"version={AppVersion}; ovrplugin={OvrPluginVersion()}; device={SystemInfo.deviceModel}; {HandSampler.BackendDescription}");
        }

        void Update()
        {
            if (Link == null) return;
            ProcessInbox();
            Sampler.Tick(SessionId, Config.allowExtrapolatedFallback);

            if (!reportedUnextrapolatedFailure && Time.realtimeSinceStartup > 5f
                && !Sampler.Stats[0].EverUnextrapolatedOk && !Sampler.Stats[1].EverUnextrapolatedOk)
            {
                reportedUnextrapolatedFailure = true;
                SendEvent("unextrapolated_unavailable",
                    "GetUnextrapolatedHandState returns false: " + HandSampler.BackendDescription
                    + "; need OpenXR backend, OpenXR hand skeleton, OVRPlugin >= 1.116.0");
            }

            bool connected = PcConnected;
            if (connected != lastConnected)
            {
                lastConnected = connected;
                SendEvent(connected ? "pc_connected" : "pc_connection_lost", Link.Target?.ToString() ?? "");
            }

            double now = MonoClock.Seconds;
            if (now >= nextStatusTime)
            {
                nextStatusTime = now + 1.0;
                SendStatus();
            }
        }

        // ---------- Связь ----------

        [Serializable] class BeaconMsg { public int pc_port; public string name; }
        [Serializable] class CommandMsg { public string cmd; public int id; public string label; }

        void ProcessInbox()
        {
            while (Link.Inbox.TryDequeue(out var msg))
            {
                byte type = msg.Data[4];
                string json = msg.Length > Proto.HeaderSize
                    ? Encoding.UTF8.GetString(msg.Data, Proto.HeaderSize, msg.Length - Proto.HeaderSize)
                    : "";
                try
                {
                    if (type == Proto.MsgBeacon) HandleBeacon(JsonUtility.FromJson<BeaconMsg>(json), msg);
                    else if (type == Proto.MsgCommand) HandleCommand(JsonUtility.FromJson<CommandMsg>(json));
                }
                catch (Exception e)
                {
                    Debug.LogWarning($"[HandStreamer] bad message type {type}: {e.Message}");
                }
            }
        }

        void HandleBeacon(BeaconMsg b, UdpLink.Incoming msg)
        {
            if (!AutoDiscovery || b == null) return;
            var ep = new IPEndPoint(msg.From.Address, b.pc_port > 0 ? b.pc_port : Proto.DefaultPcPort);
            var cur = Link.Target;
            bool currentSilent = msg.MonoTime - lastBeaconMono > 5.0;
            if (cur == null || cur.Equals(ep) || currentSilent)
            {
                if (cur == null || !cur.Equals(ep))
                {
                    Link.SetTarget(ep);
                    SendEvent("pc_discovered", $"{ep} {b.name}");
                }
                PcName = b.name ?? "";
                lastBeaconMono = msg.MonoTime;
            }
        }

        void HandleCommand(CommandMsg c)
        {
            if (c == null) return;
            SendEvent("command_received", $"{c.cmd} id={c.id}");
            switch (c.cmd)
            {
                case "start": StartRecording(c.label ?? ""); break;
                case "stop": StopRecording(); break;
            }
        }

        public void ApplyManualTarget()
        {
            if (Link == null) return;
            if (AutoDiscovery)
            {
                Link.SetTarget(null);
                PcName = "";
                return;
            }
            if (IPAddress.TryParse(Config.pcIp, out var ip))
            {
                Link.SetTarget(new IPEndPoint(ip, Config.pcPort));
                PcName = "manual";
            }
        }

        public void SetManualIp(string ip)
        {
            Config.pcIp = ip ?? "";
            Config.Save();
            ApplyManualTarget();
            SendEvent("config_changed", AutoDiscovery ? "pc_ip=auto" : "pc_ip=" + Config.pcIp);
        }

        public void ToggleSkeletonOverlay()
        {
            bool on = !SkeletonOverlay.Visible;
            SkeletonOverlay.Visible = on;
            Config.showSkeletonOverlay = on;
            Config.Save();
            SendEvent("config_changed", "skeleton_overlay=" + (on ? "on" : "off"));
        }

        // ---------- Запись ----------

        public void ToggleRecording()
        {
            if (Recording) StopRecording(); else StartRecording("");
        }

        public void StartRecording(string label)
        {
            if (Recording) return;
            var bytes = new byte[8];
            new System.Random().NextBytes(bytes);
            ulong id = BitConverter.ToUInt64(bytes, 0) | 1UL;
            string name = $"{DateTime.Now:yyyyMMdd_HHmmss}_{id:x16}.qhslog";
            string path = Path.Combine(Application.persistentDataPath, "recordings", name);
            if (label.Length > 64) label = label.Substring(0, 64);
            sessionLabel = label;
            try
            {
                recorder = new LogWriter(path, BuildMetadataJson(id, label));
            }
            catch (Exception e)
            {
                recorder = null;
                SendEvent("recording_error", e.Message);
                return;
            }
            SessionId = id;
            RecordingStartMono = MonoClock.Seconds;
            SendEvent("recording_started", $"file={name}; label={label}");
            SendStatus();
        }

        public void StopRecording()
        {
            if (!Recording) return;
            SendEvent("recording_stopped", $"duration_s={MonoClock.Seconds - RecordingStartMono:F1}");
            var r = recorder;
            recorder = null;
            SessionId = 0;
            r?.Dispose();
            SendStatus(); // уже с recording=false, чтобы ПК не открыл сессию повторно
        }

        void OnApplicationPause(bool paused)
        {
            // Снятие шлема/системное меню. Запись не прерывается, но событие фиксируется.
            SendEvent(paused ? "app_paused" : "app_resumed", "");
        }

        void OnApplicationFocus(bool focus)
        {
            SendEvent(focus ? "focus_gained" : "focus_lost", "");
        }

        void OnDestroy()
        {
            StopRecording();
            Link?.Dispose();
        }

        // ---------- Сообщения ----------

        [Serializable]
        class EventMsg
        {
            public string code;
            public string session;
            public double mono;
            public double ovr;
            public string detail;
        }

        public void SendEvent(string code, string detail)
        {
            Debug.Log($"[HandStreamer] event {code}: {detail}");
            if (Link == null) return;
            var e = new EventMsg
            {
                code = code,
                session = SessionId.ToString("x16"),
                mono = MonoClock.Seconds,
                ovr = OVRPlugin.GetTimeInSeconds(),
                detail = detail
            };
            SendJson(Proto.MsgEvent, JsonUtility.ToJson(e));
        }

        [Serializable]
        class HandStatusMsg
        {
            public float rate_hz;
            public bool unextrapolated_ok;
            public bool tracked;
            public bool high_confidence;
            public bool positions;
            public int bones;
            public int source;
            public long new_samples;
            public long duplicates;
            public long out_of_order;
            public long call_failures;
            public long fallback_samples;
            public float capture_to_send_ms;
        }

        [Serializable]
        class StatusMsg
        {
            public string app_version;
            public string device;
            public string ovrplugin;
            public string backend;
            public string session;
            public bool recording;
            public string label;
            public float recording_s;
            public long rec_bytes;
            public long rec_dropped;
            public float display_hz;
            public float battery;
            public bool allow_fallback;
            public bool auto_discovery;
            public string target;
            public long packets_sent;
            public long send_errors;
            public long not_connected_drops;
            public double mono;
            public double ovr;
            public HandStatusMsg left;
            public HandStatusMsg right;
        }

        void SendStatus()
        {
            double mono = MonoClock.Seconds;
            var s = new StatusMsg
            {
                app_version = AppVersion,
                device = SystemInfo.deviceModel,
                ovrplugin = OvrPluginVersion(),
                backend = HandSampler.BackendDescription,
                session = SessionId.ToString("x16"),
                recording = Recording,
                label = sessionLabel,
                recording_s = Recording ? (float)(mono - RecordingStartMono) : 0f,
                rec_bytes = recorder?.BytesWritten ?? 0,
                rec_dropped = recorder?.Dropped ?? 0,
                display_hz = DisplayHz,
                battery = SystemInfo.batteryLevel,
                allow_fallback = Config.allowExtrapolatedFallback,
                auto_discovery = AutoDiscovery,
                target = Link.Target?.ToString() ?? "",
                packets_sent = Link.PacketsSent,
                send_errors = Link.SendErrors,
                not_connected_drops = Link.NotConnectedDrops,
                mono = mono,
                ovr = OVRPlugin.GetTimeInSeconds(),
                left = HandStatus(Sampler.Stats[0], mono),
                right = HandStatus(Sampler.Stats[1], mono)
            };
            SendJson(Proto.MsgStatus, JsonUtility.ToJson(s));
        }

        static HandStatusMsg HandStatus(HandSampler.HandStats st, double now)
        {
            return new HandStatusMsg
            {
                rate_hz = st.Rate.Hz(now),
                unextrapolated_ok = st.UnextrapolatedOk,
                tracked = st.Tracked,
                high_confidence = st.HighConfidence,
                positions = st.PositionsPresent,
                bones = st.BoneCount,
                source = st.LastSource,
                new_samples = st.NewSamples,
                duplicates = st.Duplicates,
                out_of_order = st.OutOfOrder,
                call_failures = st.CallFailures,
                fallback_samples = st.FallbackSamples,
                capture_to_send_ms = (float)st.LastCaptureToSendMs
            };
        }

        void SendJson(byte type, string json)
        {
            int maxChars = Proto.MaxDatagram - Proto.HeaderSize;
            if (Encoding.UTF8.GetByteCount(json) > maxChars)
            {
                Debug.LogWarning($"[HandStreamer] JSON message type {type} too large, not sent");
                return;
            }
            jsonWriter.Header(type);
            jsonWriter.Json(json);
            if (!Link.Send(jsonWriter.Buf, jsonWriter.Pos))
                recorder?.Write(1, MonoClock.Nanoseconds, jsonWriter.Buf, jsonWriter.Pos);
        }

        [Serializable]
        class RecordingMeta
        {
            public string format = "qhslog";
            public int protocol_version = Proto.Version;
            public string writer = "quest";
            public string app_version = AppVersion;
            public string session;
            public string label;
            public string created_local;
            public string created_utc;
            public string device;
            public string os;
            public string ovrplugin;
            public string unity;
            public float display_hz;
            public bool allow_extrapolated_fallback;
            public string xr_backend;
            public string time_ns_domain = "mono (Stopwatch) ns";
            public string coordinates = "raw OVRPlugin Posef/Quatf/Vector3f, not converted to Unity";
        }

        string BuildMetadataJson(ulong id, string label)
        {
            var m = new RecordingMeta
            {
                session = id.ToString("x16"),
                label = label,
                created_local = DateTime.Now.ToString("o"),
                created_utc = DateTime.UtcNow.ToString("o"),
                device = SystemInfo.deviceModel,
                os = SystemInfo.operatingSystem,
                ovrplugin = OvrPluginVersion(),
                unity = Application.unityVersion,
                display_hz = DisplayHz,
                allow_extrapolated_fallback = Config.allowExtrapolatedFallback,
                xr_backend = HandSampler.BackendDescription
            };
            return JsonUtility.ToJson(m, true);
        }

        void ApplyDisplayFrequency()
        {
            try
            {
                if (Config.displayFrequency > 0)
                {
                    foreach (float f in OVRPlugin.systemDisplayFrequenciesAvailable)
                    {
                        if (Mathf.Approximately(f, Config.displayFrequency))
                        {
                            OVRPlugin.systemDisplayFrequency = f;
                            break;
                        }
                    }
                }
                DisplayHz = OVRPlugin.systemDisplayFrequency;
            }
            catch (Exception e)
            {
                Debug.LogWarning("[HandStreamer] display frequency: " + e.Message);
            }
        }

        public static string OvrPluginVersion()
        {
            try { return OVRPlugin.version?.ToString() ?? "unknown"; }
            catch { return "unknown"; }
        }
    }
}
