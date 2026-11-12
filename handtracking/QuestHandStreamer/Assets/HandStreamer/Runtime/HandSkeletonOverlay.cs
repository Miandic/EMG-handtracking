using UnityEngine;

namespace HandStreamer
{
    /// <summary>
    /// Наложение считываемого скелета поверх рук в реальном времени — те же данные, что уходят в запись
    /// (непредсказанная поза на момент захвата, 26 суставов OpenXR). Поэтому скелет немного отстаёт от
    /// реальной руки на быстрых движениях: это видимая задержка конвейера трекинга (~25–45 мс).
    /// Цвета как в просмотрщике на ПК: левая — синяя, правая — оранжевая, низкая уверенность — жёлтая,
    /// нет трекинга / устаревшие данные — серая.
    /// </summary>
    public sealed class HandSkeletonOverlay : MonoBehaviour
    {
        // Рёбра скелета OpenXR (как в pc-receiver/viewer.py)
        static readonly int[,] Edges =
        {
            { 1, 2 }, { 2, 3 }, { 3, 4 }, { 4, 5 },                       // большой
            { 1, 6 }, { 6, 7 }, { 7, 8 }, { 8, 9 }, { 9, 10 },            // указательный
            { 1, 11 }, { 11, 12 }, { 12, 13 }, { 13, 14 }, { 14, 15 },    // средний
            { 1, 16 }, { 16, 17 }, { 17, 18 }, { 18, 19 }, { 19, 20 },    // безымянный
            { 1, 21 }, { 21, 22 }, { 22, 23 }, { 23, 24 }, { 24, 25 },    // мизинец
            { 7, 12 }, { 12, 17 }, { 17, 22 },                            // линия пястно-фаланговых суставов
        };
        const int JointCount = 26;
        const double StaleSeconds = 0.15, HideSeconds = 1.0;

        static readonly Color[] HandColors = { new Color(0.31f, 0.63f, 1f, 0.9f), new Color(1f, 0.6f, 0.24f, 0.9f) };
        static readonly Color LowConfidence = new Color(0.85f, 0.75f, 0.3f, 0.9f);
        static readonly Color Inactive = new Color(0.55f, 0.57f, 0.6f, 0.6f);

        sealed class HandVisual
        {
            public GameObject Root;
            public LineRenderer[] Bones;
            public Transform[] Joints;
            public Material JointMaterial;
        }

        HandSampler sampler;
        Transform trackingSpace;
        readonly HandVisual[] hands = new HandVisual[2];
        bool built;
        bool visible;

        public bool Visible
        {
            get => visible;
            set
            {
                visible = value;
                if (built)
                    foreach (var h in hands) h.Root.SetActive(value);
            }
        }

        public void Init(HandSampler handSampler, bool startVisible)
        {
            sampler = handSampler;
            visible = startVisible;
        }

        bool TryBuild()
        {
            var rig = FindFirstObjectByType<OVRCameraRig>();
            if (rig == null || rig.trackingSpace == null) return false;
            trackingSpace = rig.trackingSpace;
            var shader = Shader.Find("UI/Default") ?? Shader.Find("Sprites/Default");
            if (shader == null)
            {
                Debug.LogWarning("[HandStreamer] skeleton overlay: shader not found");
                enabled = false;
                return false;
            }
            var lineMat = new Material(shader);
            for (int h = 0; h < 2; h++)
            {
                var v = new HandVisual { Root = new GameObject(h == 0 ? "Skeleton L" : "Skeleton R") };
                v.Root.transform.SetParent(transform, false);
                v.Bones = new LineRenderer[Edges.GetLength(0)];
                for (int e = 0; e < v.Bones.Length; e++)
                {
                    var lr = new GameObject("Bone").AddComponent<LineRenderer>();
                    lr.transform.SetParent(v.Root.transform, false);
                    lr.useWorldSpace = true;
                    lr.positionCount = 2;
                    lr.startWidth = lr.endWidth = 0.0035f;
                    lr.sharedMaterial = lineMat;
                    lr.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
                    lr.receiveShadows = false;
                    v.Bones[e] = lr;
                }
                v.JointMaterial = new Material(shader);
                v.Joints = new Transform[JointCount];
                for (int j = 0; j < JointCount; j++)
                {
                    var go = GameObject.CreatePrimitive(PrimitiveType.Sphere);
                    Destroy(go.GetComponent<Collider>());
                    go.name = "Joint " + j;
                    go.transform.SetParent(v.Root.transform, false);
                    float d = j <= 1 ? 0.012f : (j == 5 || j == 10 || j == 15 || j == 20 || j == 25 ? 0.009f : 0.007f);
                    go.transform.localScale = Vector3.one * d;
                    var mr = go.GetComponent<MeshRenderer>();
                    mr.sharedMaterial = v.JointMaterial;
                    mr.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
                    mr.receiveShadows = false;
                    v.Joints[j] = go.transform;
                }
                v.Root.SetActive(visible);
                hands[h] = v;
            }
            built = true;
            return true;
        }

        void LateUpdate()
        {
            if (sampler == null || !visible) return;
            if (!built && !TryBuild()) return;

            double now = MonoClock.Seconds;
            for (int h = 0; h < 2; h++)
            {
                var st = sampler.Stats[h];
                var v = hands[h];
                double age = now - st.LastSampleMono;
                bool show = st.JointCount >= JointCount && age < HideSeconds;
                if (v.Root.activeSelf != show) v.Root.SetActive(show);
                if (!show) continue;

                Color c = !st.Tracked || age > StaleSeconds ? Inactive
                        : st.HighConfidence ? HandColors[h] : LowConfidence;
                v.JointMaterial.color = c;

                for (int j = 0; j < JointCount; j++)
                    v.Joints[j].position = trackingSpace.TransformPoint(st.Joints[j]);

                for (int e = 0; e < v.Bones.Length; e++)
                {
                    var lr = v.Bones[e];
                    lr.SetPosition(0, v.Joints[Edges[e, 0]].position);
                    lr.SetPosition(1, v.Joints[Edges[e, 1]].position);
                    lr.startColor = lr.endColor = c;
                }
            }
        }
    }
}
