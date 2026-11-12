using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.UI;

namespace HandStreamer
{
    /// <summary>
    /// HUD, жёстко закреплённый за головой (дочерние объекты CenterEyeAnchor). Элементы вынесены к краям
    /// поля зрения: статус — вверху слева, запись — вверху справа, кнопки — внизу (скрываемые).
    ///
    /// Управление без контроллеров:
    ///  • навести луч руки на кнопку и сделать щипок большим и указательным пальцами;
    ///  • жест «меню» левой рукой (ладонь к себе + щипок) — показать/скрыть кнопки;
    ///  • при скрытых кнопках внизу остаётся маленькая кнопка «Меню».
    /// Луч рисуется только когда он указывает на элемент HUD. Данные рук для интерфейса берутся из
    /// экстраполированного состояния (Step.Render) и в запись не попадают.
    /// Положение элементов задаётся в config.json (hud*).
    /// </summary>
    public sealed class HandStreamerHud : MonoBehaviour
    {
        const float MetersPerPixel = 0.0005f;
        static readonly Color ColPanel = new Color(0.04f, 0.05f, 0.07f, 0.55f);
        static readonly Color ColButton = new Color(0.18f, 0.22f, 0.28f, 0.92f);
        static readonly Color ColHover = new Color(0.30f, 0.45f, 0.65f, 0.95f);
        static readonly Color ColRecord = new Color(0.70f, 0.14f, 0.14f, 0.95f);

        sealed class Panel
        {
            public RectTransform Rect;
            public bool Interactive;
            public RectTransform Cursor;
            public GameObject Go => Rect.gameObject;
        }

        sealed class Button
        {
            public Panel Panel;
            public RectTransform Rect;
            public Image Image;
            public Text Label;
            public Action OnClick;
        }

        HandStreamerApp app;
        StreamerConfig cfg;
        Font font;
        Transform head, trackingSpace;
        bool built;

        Panel statusPanel, recPanel, controlsPanel, handlePanel, keypadPanel;
        Text statusText, recText, ipText;
        Button recordButton, skeletonButton;
        readonly List<Panel> panels = new List<Panel>();
        readonly List<Button> buttons = new List<Button>();
        readonly LineRenderer[] rays = new LineRenderer[2];

        OVRPlugin.HandState uiState = new OVRPlugin.HandState();
        readonly bool[] wasPinching = new bool[2];
        readonly bool[] wasMenu = new bool[2];
        // Кнопка под лучом каждой руки в последние кадры: при щипке кисть смещается и луч может
        // соскользнуть с кнопки до того, как сработает распознавание щипка.
        readonly Button[] lastHover = new Button[2];
        readonly float[] lastHoverTime = { -10f, -10f };
        const float HoverLatchSeconds = 0.3f;
        // Щипок по скелету: расстояние между кончиками большого (5) и указательного (10) пальцев.
        // При OpenXR-скелете OVRPlugin не заполняет Pinches/PinchStrength (проверено на Quest 3, SDK 209).
        const int ThumbTip = 5, IndexTip = 10, PalmJoint = 0;
        const float TipPinchOnM = 0.016f, TipPinchOffM = 0.026f;
        // Жест «меню»: ладонь повёрнута к лицу не менее MenuFacingSeconds, затем щипок.
        const float MenuFacingDot = 0.6f, MenuFacingSeconds = 0.25f;
        readonly float[] facingSince = { -1f, -1f };
        const float PinchOn = 0.8f, PinchOff = 0.5f;
        bool controlsVisible;
        bool keypadOpen;
        bool wasRecording;
        string ipDraft = "";
        float nextTextUpdate;
        readonly StringBuilder sb = new StringBuilder(512);

        public void Init(HandStreamerApp owner)
        {
            app = owner;
            cfg = owner.Config;
            font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
            controlsVisible = cfg.hudControlsVisibleOnStart;
        }

        // ---------- построение ----------

        bool TryBuild()
        {
            var rig = FindFirstObjectByType<OVRCameraRig>();
            if (rig == null || rig.centerEyeAnchor == null) return false;
            head = rig.centerEyeAnchor;
            trackingSpace = rig.trackingSpace;

            float d = cfg.hudDistance;

            statusPanel = NewPanel("HUD Status", cfg.hudStatusYaw, cfg.hudStatusPitch, d, new Vector2(560, 170), false);
            statusText = NewText(statusPanel.Rect, new Vector2(16, 10), new Vector2(528, 150), 26, TextAnchor.UpperLeft);

            recPanel = NewPanel("HUD Recording", -cfg.hudStatusYaw, cfg.hudStatusPitch, d, new Vector2(440, 130), false);
            recText = NewText(recPanel.Rect, new Vector2(16, 10), new Vector2(408, 110), 30, TextAnchor.UpperRight);

            controlsPanel = NewPanel("HUD Controls", 0, cfg.hudControlsPitch, d, new Vector2(1060, 130), true);
            recordButton = NewButton(controlsPanel, "● ЗАПИСЬ", new Vector2(15, 15), new Vector2(280, 100), () => app.ToggleRecording());
            NewButton(controlsPanel, "Адрес ПК", new Vector2(310, 15), new Vector2(240, 100), OpenKeypad);
            skeletonButton = NewButton(controlsPanel, "Скелет", new Vector2(565, 15), new Vector2(285, 100), () => app.ToggleSkeletonOverlay());
            NewButton(controlsPanel, "Скрыть", new Vector2(865, 15), new Vector2(180, 100), () => SetControlsVisible(false));

            handlePanel = NewPanel("HUD Menu Handle", cfg.hudHandleYaw, cfg.hudControlsPitch, d, new Vector2(190, 80), true, 0.35f);
            NewButton(handlePanel, "Меню", new Vector2(8, 8), new Vector2(174, 64), () => SetControlsVisible(true), 28);

            keypadPanel = NewPanel("HUD Keypad", 0, -10, d * 0.8f, new Vector2(980, 600), true, 0.8f);
            ipText = NewText(keypadPanel.Rect, new Vector2(30, 20), new Vector2(920, 60), 36, TextAnchor.UpperLeft);
            string[] keys = { "1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "0", "←" };
            for (int i = 0; i < keys.Length; i++)
            {
                string k = keys[i];
                NewButton(keypadPanel, k, new Vector2(30 + (i % 3) * 150, 100 + (i / 3) * 120), new Vector2(140, 110), () => KeyPressed(k), 40);
            }
            NewButton(keypadPanel, "Сохранить", new Vector2(520, 100), new Vector2(430, 110), SaveIp);
            NewButton(keypadPanel, "Автопоиск ПК", new Vector2(520, 230), new Vector2(430, 110), () => { app.SetManualIp(""); CloseKeypad(); });
            NewButton(keypadPanel, "Отмена", new Vector2(520, 360), new Vector2(430, 110), CloseKeypad);

            var shader = Shader.Find("UI/Default") ?? Shader.Find("Sprites/Default");
            if (shader != null)
            {
                for (int h = 0; h < 2; h++)
                {
                    var lr = new GameObject(h == 0 ? "HUD Ray L" : "HUD Ray R").AddComponent<LineRenderer>();
                    lr.useWorldSpace = true;
                    lr.positionCount = 2;
                    lr.startWidth = 0.004f;
                    lr.endWidth = 0.0015f;
                    lr.material = new Material(shader);
                    lr.startColor = new Color(1f, 1f, 1f, 0.05f);
                    lr.endColor = new Color(0.7f, 0.85f, 1f, 0.8f);
                    lr.numCapVertices = 2;
                    lr.enabled = false;
                    rays[h] = lr;
                }
            }

            ApplyVisibility();
            built = true;
            return true;
        }

        Panel NewPanel(string name, float yawDeg, float pitchDeg, float distance, Vector2 sizePx, bool interactive, float bgAlphaScale = 1f)
        {
            var go = new GameObject(name, typeof(RectTransform), typeof(Canvas), typeof(CanvasScaler));
            var rt = (RectTransform)go.transform;
            rt.SetParent(head, false);
            var canvas = go.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.WorldSpace;
            canvas.sortingOrder = 100;
            go.GetComponent<CanvasScaler>().dynamicPixelsPerUnit = 3f;
            rt.sizeDelta = sizePx;
            rt.localScale = Vector3.one * MetersPerPixel;
            // Направление на элемент из глаза: yaw — вправо, pitch — вверх.
            Vector3 dir = Quaternion.Euler(-pitchDeg, yawDeg, 0f) * Vector3.forward;
            rt.localPosition = dir * distance;
            rt.localRotation = Quaternion.LookRotation(dir, Vector3.up);

            var bg = NewRect("Background", rt, Vector2.zero, sizePx);
            var c = ColPanel;
            c.a *= bgAlphaScale;
            bg.gameObject.AddComponent<Image>().color = c;

            var p = new Panel { Rect = rt, Interactive = interactive };
            if (interactive)
            {
                p.Cursor = NewRect("Cursor", rt, Vector2.zero, new Vector2(16, 16));
                p.Cursor.pivot = new Vector2(0.5f, 0.5f);
                p.Cursor.gameObject.AddComponent<Image>().color = new Color(1f, 1f, 1f, 0.9f);
                p.Cursor.gameObject.SetActive(false);
            }
            panels.Add(p);
            return p;
        }

        static RectTransform NewRect(string name, RectTransform parent, Vector2 topLeft, Vector2 size)
        {
            var go = new GameObject(name, typeof(RectTransform));
            var rt = (RectTransform)go.transform;
            rt.SetParent(parent, false);
            rt.anchorMin = rt.anchorMax = new Vector2(0, 1);
            rt.pivot = new Vector2(0, 1);
            rt.anchoredPosition = new Vector2(topLeft.x, -topLeft.y);
            rt.sizeDelta = size;
            return rt;
        }

        Text NewText(RectTransform parent, Vector2 topLeft, Vector2 size, int fontSize, TextAnchor anchor)
        {
            var t = NewRect("Text", parent, topLeft, size).gameObject.AddComponent<Text>();
            t.font = font;
            t.fontSize = fontSize;
            t.alignment = anchor;
            t.color = Color.white;
            t.horizontalOverflow = HorizontalWrapMode.Wrap;
            t.verticalOverflow = VerticalWrapMode.Truncate;
            t.supportRichText = true;
            t.lineSpacing = 1.05f;
            return t;
        }

        Button NewButton(Panel panel, string label, Vector2 topLeft, Vector2 size, Action onClick, int fontSize = 32)
        {
            var rt = NewRect("Button " + label, panel.Rect, topLeft, size);
            var img = rt.gameObject.AddComponent<Image>();
            img.color = ColButton;
            var text = NewText(rt, Vector2.zero, size, fontSize, TextAnchor.MiddleCenter);
            text.fontStyle = FontStyle.Bold;
            text.text = label;
            var b = new Button { Panel = panel, Rect = rt, Image = img, Label = text, OnClick = onClick };
            buttons.Add(b);
            return b;
        }

        // ---------- состояние ----------

        public void SetControlsVisible(bool visible)
        {
            controlsVisible = visible;
            if (!visible) keypadOpen = false;
            ApplyVisibility();
        }

        void ApplyVisibility()
        {
            if (controlsPanel == null) return;
            controlsPanel.Go.SetActive(controlsVisible && !keypadOpen);
            handlePanel.Go.SetActive(!controlsVisible && !keypadOpen);
            keypadPanel.Go.SetActive(keypadOpen);
        }

        void OpenKeypad()
        {
            ipDraft = app.AutoDiscovery ? "" : app.Config.pcIp;
            keypadOpen = true;
            ApplyVisibility();
        }

        void CloseKeypad()
        {
            keypadOpen = false;
            ApplyVisibility();
        }

        void KeyPressed(string k)
        {
            if (k == "←") { if (ipDraft.Length > 0) ipDraft = ipDraft.Substring(0, ipDraft.Length - 1); }
            else if (ipDraft.Length < 15) ipDraft += k;
        }

        void SaveIp()
        {
            if (System.Net.IPAddress.TryParse(ipDraft, out _) && ipDraft.Split('.').Length == 4)
            {
                app.SetManualIp(ipDraft);
                CloseKeypad();
            }
            else
            {
                ipDraft = "";
            }
        }

        // ---------- кадр ----------

        void LateUpdate()
        {
            if (app == null) return;
            if (!built && !TryBuild()) return;

            if (app.Recording && !wasRecording && cfg.hudAutoHideControlsOnRecord) SetControlsVisible(false);
            wasRecording = app.Recording;

            HandleHands();

            if (Time.unscaledTime >= nextTextUpdate)
            {
                nextTextUpdate = Time.unscaledTime + 0.2f;
                UpdateTexts();
            }
        }

        void HandleHands()
        {
            Button hovered = null;
            Button clickTarget = null;
            Panel hoveredPanel = null;
            Vector3 hoverPoint = Vector3.zero;
            float now = Time.unscaledTime;

            for (int h = 0; h < 2; h++)
            {
                var hand = h == 0 ? OVRPlugin.Hand.HandLeft : OVRPlugin.Hand.HandRight;
                bool pinching = false, menu = false, flag = false, sysGesture = false, inputValid = false, facing = false;
                float strength = 0f, tipDist = float.PositiveInfinity;
                Vector3 origin = Vector3.zero, hit = Vector3.zero;
                Panel hitPanel = null;

                if (trackingSpace != null && OVRPlugin.GetHandState(OVRPlugin.Step.Render, hand, ref uiState))
                {
                    var st = uiState.Status;
                    menu = (st & OVRPlugin.HandStatus.MenuPressed) != 0;
                    sysGesture = (st & OVRPlugin.HandStatus.SystemGestureInProgress) != 0;
                    inputValid = (st & OVRPlugin.HandStatus.InputStateValid) != 0;
                    flag = (uiState.Pinches & OVRPlugin.HandFingerPinch.Index) != 0;
                    var ps = uiState.PinchStrength;
                    int idx = (int)OVRPlugin.HandFinger.Index;
                    strength = ps != null && ps.Length > idx ? ps[idx] : 0f;
                    bool tracked = (st & OVRPlugin.HandStatus.HandTracked) != 0;
                    float scale = uiState.HandScale > 0.5f && uiState.HandScale < 2f ? uiState.HandScale : 1f;
                    if (tracked)
                    {
                        tipDist = TipDistance(uiState);
                        facing = PalmFacesHead(uiState);
                    }
                    bool skeletonPinch = tipDist < (wasPinching[h] ? TipPinchOffM : TipPinchOnM) * scale;
                    // Щипок: по скелету, либо системный флаг/сила (если рантайм их передаёт).
                    pinching = !sysGesture && (skeletonPinch || flag || strength >= (wasPinching[h] ? PinchOff : PinchOn));
                    if (inputValid && !sysGesture)
                    {
                        OVRPose p = uiState.PointerPose.ToOVRPose();
                        origin = trackingSpace.TransformPoint(p.position);
                        var ray = new Ray(origin, trackingSpace.rotation * (p.orientation * Vector3.forward));
                        float best = 3f;
                        foreach (var panel in panels)
                        {
                            if (!panel.Interactive || !panel.Go.activeInHierarchy) continue;
                            var plane = new Plane(panel.Rect.forward, panel.Rect.position);
                            if (!plane.Raycast(ray, out float enter) || enter > best) continue;
                            Vector3 point = ray.GetPoint(enter);
                            Vector2 local = panel.Rect.InverseTransformPoint(point);
                            if (!panel.Rect.rect.Contains(local)) continue;
                            best = enter;
                            hitPanel = panel;
                            hit = point;
                        }
                    }
                }

                // Жест «меню»: системный MenuPressed (если приходит) — по фронту сигнала.
                if (menu && !wasMenu[h]) ToggleMenu("system MenuPressed");
                wasMenu[h] = menu;

                // Собственный жест «меню»: ладонь к лицу дольше MenuFacingSeconds + щипок.
                if (facing) { if (facingSince[h] < 0) facingSince[h] = now; }
                else facingSince[h] = -1f;
                bool menuPose = facing && now - facingSince[h] >= MenuFacingSeconds;

                Button handHover = hitPanel != null ? HitButton(hitPanel, hit) : null;
                if (handHover != null)
                {
                    lastHover[h] = handHover;
                    lastHoverTime[h] = now;
                }
                if (hitPanel != null && hoveredPanel == null)
                {
                    hoveredPanel = hitPanel;
                    hoverPoint = hit;
                    hovered = handHover;
                }

                if (pinching && !wasPinching[h] && menuPose)
                {
                    Debug.Log($"[HandStreamer] ui menu gesture {(h == 0 ? "L" : "R")}: tip={tipDist * 100f:F1}cm");
                    ToggleMenu("palm gesture");
                }
                else if (pinching && !wasPinching[h])
                {
                    Button target = handHover;
                    bool latched = false;
                    if (target == null && lastHover[h] != null && now - lastHoverTime[h] <= HoverLatchSeconds
                        && lastHover[h].Rect.gameObject.activeInHierarchy)
                    {
                        target = lastHover[h];
                        latched = true;
                    }
                    if (target != null && clickTarget == null) clickTarget = target;
                    Debug.Log($"[HandStreamer] ui pinch {(h == 0 ? "L" : "R")}: tip={tipDist * 100f:F1}cm flag={flag} strength={strength:F2} " +
                              $"inputValid={inputValid} sysGesture={sysGesture} hover={handHover?.Label.text ?? "-"} " +
                              $"target={(target != null ? target.Label.text + (latched ? " (latched)" : "") : "-")}");
                }

                var lr = rays[h];
                if (lr != null)
                {
                    bool show = hitPanel != null;
                    lr.enabled = show;
                    if (show)
                    {
                        lr.SetPosition(0, origin);
                        lr.SetPosition(1, hit);
                    }
                }
                wasPinching[h] = pinching;
            }

            foreach (var panel in panels)
            {
                if (panel.Cursor == null) continue;
                bool on = panel == hoveredPanel;
                panel.Cursor.gameObject.SetActive(on);
                if (on)
                {
                    Vector3 local = panel.Rect.InverseTransformPoint(hoverPoint);
                    panel.Cursor.localPosition = new Vector3(local.x, local.y, -1f);
                }
            }

            foreach (var b in buttons)
            {
                Color baseColor = b == recordButton && app.Recording ? ColRecord : ColButton;
                b.Image.color = b == hovered ? ColHover : baseColor;
            }

            if (clickTarget != null)
            {
                for (int h = 0; h < 2; h++) lastHover[h] = null; // один щипок — одно нажатие
                clickTarget.OnClick?.Invoke();
            }
        }

        void ToggleMenu(string source)
        {
            if (keypadOpen) CloseKeypad();
            else SetControlsVisible(!controlsVisible);
            Debug.Log($"[HandStreamer] ui menu toggled by {source}: controls={(controlsVisible ? "shown" : "hidden")}");
        }

        static float TipDistance(OVRPlugin.HandState s)
        {
            var pos = s.BonePositions;
            if (pos == null || pos.Length <= IndexTip) return float.PositiveInfinity;
            var a = pos[ThumbTip];
            var b = pos[IndexTip];
            float dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
            return Mathf.Sqrt(dx * dx + dy * dy + dz * dz);
        }

        /// <summary>
        /// Ладонь повёрнута к лицу. Нормаль ладони — −Y сустава Palm (OpenXR: +Y — тыл кисти, +Z — к запястью;
        /// подтверждено на записанных данных). Сырые координаты OVRPlugin переводятся в Unity через ToOVRPose().
        /// </summary>
        bool PalmFacesHead(OVRPlugin.HandState s)
        {
            var pos = s.BonePositions;
            var rot = s.BoneRotations;
            if (pos == null || rot == null || pos.Length <= PalmJoint || rot.Length <= PalmJoint || head == null) return false;
            var raw = new OVRPlugin.Posef { Position = pos[PalmJoint], Orientation = rot[PalmJoint] };
            OVRPose palm = raw.ToOVRPose();
            Vector3 palmWorld = trackingSpace.TransformPoint(palm.position);
            Vector3 normalWorld = trackingSpace.rotation * (palm.orientation * Vector3.down);
            Vector3 toHead = (head.position - palmWorld).normalized;
            return Vector3.Dot(normalWorld, toHead) >= MenuFacingDot;
        }

        static Button HitButton(Panel panel, Vector3 world, List<Button> list)
        {
            foreach (var b in list)
            {
                if (b.Panel != panel || !b.Rect.gameObject.activeInHierarchy) continue;
                Vector2 lp = b.Rect.InverseTransformPoint(world);
                if (b.Rect.rect.Contains(lp)) return b;
            }
            return null;
        }

        Button HitButton(Panel panel, Vector3 world) => HitButton(panel, world, buttons);

        // ---------- тексты ----------

        void UpdateTexts()
        {
            recordButton.Label.text = app.Recording ? "■ СТОП" : "● ЗАПИСЬ";
            skeletonButton.Label.text = app.SkeletonOverlay != null && app.SkeletonOverlay.Visible ? "Скелет: вкл" : "Скелет: выкл";
            ipText.text = "IP компьютера: " + (ipDraft.Length > 0 ? ipDraft : "_");

            // Статус (вверху слева)
            sb.Clear();
            if (!string.IsNullOrEmpty(app.StartupError)) sb.Append("<color=#ff6060>").Append(app.StartupError).Append("</color>\n");
            var target = app.Link?.Target;
            sb.Append("ПК ");
            if (target == null) sb.Append(app.AutoDiscovery ? "<color=#ffb040>поиск…</color>" : "<color=#ffb040>адрес не задан</color>");
            else sb.Append(target.Address).Append(app.PcConnected ? "  <color=#60e060>связь</color>" : "  <color=#ffb040>нет связи</color>");
            sb.Append('\n');

            double now = MonoClock.Seconds;
            for (int h = 0; h < 2; h++)
            {
                var st = app.Sampler.Stats[h];
                sb.Append(h == 0 ? "Л " : "   П ");
                if (!st.UnextrapolatedOk && !HandSampler.UnextrapolatedSupported)
                    sb.Append("<color=#ff6060>недоступно</color>");
                else
                {
                    string col = st.Tracked ? (st.HighConfidence ? "#60e060" : "#e0d060") : "#ffb040";
                    sb.Append("<color=").Append(col).Append('>').Append(st.Rate.Hz(now).ToString("F0")).Append(" Гц</color>");
                    if (st.LastSource == Proto.SourceExtrapolated) sb.Append("<color=#ffb040> экстрап.</color>");
                }
            }
            sb.Append('\n');
            sb.Append("<color=#9aa0aa><size=22>пакетов ").Append(app.Link?.PacketsSent ?? 0);
            if (app.Link != null && app.Link.SendErrors > 0) sb.Append(" · ошибок ").Append(app.Link.SendErrors);
            sb.Append(" · ").Append(app.DisplayHz.ToString("F0")).Append(" Гц");
            if (!controlsVisible && !keypadOpen) sb.Append("\nменю: ладонь к лицу + щипок");
            sb.Append("</size></color>");
            statusText.text = sb.ToString();

            // Запись (вверху справа)
            sb.Clear();
            if (app.Recording)
            {
                double dur = now - app.RecordingStartMono;
                long bytes = app.Recorder?.BytesWritten ?? 0;
                sb.Append("<color=#ff4040>● ЗАПИСЬ</color> ").Append(TimeSpan.FromSeconds(dur).ToString(@"hh\:mm\:ss"));
                sb.Append("\n<color=#9aa0aa><size=22>").Append((bytes / 1048576.0).ToString("F1")).Append(" МБ · ")
                  .Append(app.SessionId.ToString("x16").Substring(0, 8)).Append("</size></color>");
                long dropped = app.Recorder?.Dropped ?? 0;
                if (dropped > 0) sb.Append("\n<color=#ff6060><size=22>потеряно ").Append(dropped).Append("</size></color>");
            }
            else
            {
                sb.Append("<color=#8a8f98>запись выкл.</color>");
            }
            recText.text = sb.ToString();
        }
    }
}
