# EMG-handtracking

Проект «УМНИК» 0117635: непрерывная оценка движений кисти по поверхностной ЭМГ
и сбор синхронных данных ЭМГ и кинематики для обучения и проверки модели.

## Структура

Существующее приложение выделено в модуль `handtracking/`:

```text
handtracking/
  QuestHandStreamer/  Unity-приложение для Meta Quest 3
  pc-receiver/        Приём по UDP, запись, конвертация и 3D-просмотр треков
  docs/protocol.md    Протокол передачи и формат записей
  README.md           Настройка, сборка, запуск и результаты проверок
```

Инструкции модуля: [Hand tracking](handtracking/README.md).
Спецификация: [Протокол](handtracking/docs/protocol.md).
Будущие модули ЭМГ и обучения модели можно добавлять рядом с `handtracking/`.

## Быстрый запуск

В Unity Hub откройте папку `handtracking/QuestHandStreamer`.
Версия редактора указана в `handtracking/QuestHandStreamer/ProjectSettings/ProjectVersion.txt`.

Приёмник на ПК (Python 3, только стандартная библиотека), из корня репозитория:

```sh
python handtracking/pc-receiver/receiver.py
```

Просмотр записей:

```sh
python handtracking/pc-receiver/viewer.py
```

Без аргумента просмотрщик предлагает выбрать файл. Записи приёмника сохраняются
в `handtracking/pc-receiver/recordings/`.

## Хранение файлов

У всего проекта один Git-репозиторий и общие `.gitignore` и `.gitattributes` в корне.
Исходники Unity, файлы `.meta`, `Packages` и `ProjectSettings` входят в репозиторий.
Кэш Unity, локальные настройки, файлы IDE, сборки и экспериментальные записи исключены из Git.
Экспериментальные данные следует архивировать отдельно.
