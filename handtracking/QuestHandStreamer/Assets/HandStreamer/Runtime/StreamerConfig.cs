using System;
using System.IO;
using UnityEngine;

namespace HandStreamer
{
    /// <summary>
    /// Настройки приложения: {persistentDataPath}/config.json.
    /// Файл можно отредактировать на Quest или загрузить через adb (см. README).
    /// </summary>
    [Serializable]
    public sealed class StreamerConfig
    {
        /// <summary>IP компьютера. Пусто — автоматический поиск по широковещательному маяку приёмника.</summary>
        public string pcIp = "";
        public int pcPort = Proto.DefaultPcPort;
        public int localPort = Proto.DefaultQuestPort;
        /// <summary>Разрешить подмену непредсказанной позы экстраполированной. По умолчанию запрещено.</summary>
        public bool allowExtrapolatedFallback = false;
        /// <summary>Запрашиваемая частота дисплея, Гц (0 — не менять).</summary>
        public float displayFrequency = 90f;

        // HUD (углы в градусах от направления взгляда; yaw > 0 — вправо, pitch > 0 — вверх)
        /// <summary>Расстояние HUD от глаз, м.</summary>
        public float hudDistance = 1.0f;
        /// <summary>Блок статуса — вверху слева; индикатор записи — зеркально справа.</summary>
        public float hudStatusYaw = -26f;
        public float hudStatusPitch = 18f;
        /// <summary>Ряд кнопок — внизу по центру; кнопка «Меню» при скрытых кнопках — внизу справа.</summary>
        public float hudControlsPitch = -26f;
        public float hudHandleYaw = 22f;
        public bool hudControlsVisibleOnStart = true;
        /// <summary>Скрывать кнопки при начале записи, чтобы не перекрывать обзор.</summary>
        public bool hudAutoHideControlsOnRecord = true;
        /// <summary>Показывать считываемый скелет поверх рук (переключается кнопкой «Скелет» в меню).</summary>
        public bool showSkeletonOverlay = false;

        public static string FilePath => Path.Combine(Application.persistentDataPath, "config.json");

        public static StreamerConfig Load()
        {
            try
            {
                if (File.Exists(FilePath))
                {
                    var cfg = JsonUtility.FromJson<StreamerConfig>(File.ReadAllText(FilePath));
                    if (cfg != null) return cfg;
                }
            }
            catch (Exception e)
            {
                Debug.LogWarning($"[HandStreamer] config.json не прочитан: {e.Message}");
            }
            var def = new StreamerConfig();
            def.Save();
            return def;
        }

        public void Save()
        {
            try
            {
                File.WriteAllText(FilePath, JsonUtility.ToJson(this, true));
            }
            catch (Exception e)
            {
                Debug.LogWarning($"[HandStreamer] config.json не сохранён: {e.Message}");
            }
        }
    }
}
