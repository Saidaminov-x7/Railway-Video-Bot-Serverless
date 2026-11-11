"""Russian user-facing bot messages and format labels."""
from __future__ import annotations

from enum import Enum


class VideoFormat(str, Enum):
    VIDEO_360P = "видео 360p"
    VIDEO_720P = "видео 720p"
    VIDEO_1080P = "видео 1080p"
    AUDIO_MP3 = "аудио MP3"


MESSAGES = {
    "ru": {
        "START_MESSAGE": (
            "👋 Привет! Я помогу скачать видео с YouTube, TikTok и Instagram.\n\n"
            "Отправьте ссылку на видео — я предложу выбрать качество "
            "(видео 360p / 720p / 1080p) или аудио MP3.\n\n"
            "📌 Отправляйте только контент, на скачивание которого у вас есть права."
        ),
        "LINK_RECEIVED_MESSAGE": "🔍 Ссылка получена. Анализирую видео...",
        "INVALID_LINK_MESSAGE": "❌ Не удалось распознать ссылку. Поддерживаются только ссылки на YouTube, TikTok и Instagram.",
        "FORMAT_PROMPT_MESSAGE": "Какой файл подготовить?",
        "FORMAT_BUTTONS": {
            "v360": "🎬 Видео 360p",
            "v720": "🎬 Видео 720p",
            "v1080": "✨ Видео 1080p",
            "mp3": "🎵 Аудио MP3",
        },
        "PLURALS": {
            "link": {"one": "ссылка", "few": "ссылки", "many": "ссылок"},
            "task": {"one": "задача", "few": "задачи", "many": "задач"},
        },
        "FORMAT_SELECTED_MESSAGE": "✅ Выбрано: {format_label}. Начинаю обработку...",
        "FORMAT_SELECTED_QUEUED_MESSAGE": "✅ Выбрано: {format_label}. Добавлено в очередь, позиция: {queue_position}.",
        "PROCESSING_MESSAGE": "⏳ Обрабатываю видео, это может занять некоторое время...",
        "SUCCESS_MESSAGE": "✅ Готово! Файл отправлен выше.",
        "ERROR_LOGIN_REQUIRED_MESSAGE": (
            "⚠️ Не удалось скачать это видео: платформа требует вход в аккаунт для доступа к этому контенту.\n"
            "Это ограничение самого видео, смена качества/формата не поможет. Попробуйте другую ссылку."
        ),
        "ERROR_GENERIC_MESSAGE": "❌ Не удалось скачать видео. Попробуйте позже или отправьте другую ссылку.",
        "ERROR_FILE_TOO_LARGE_MESSAGE": "⚠️ Видео слишком большое для отправки в Telegram (лимит {limit_mb} МБ). Попробуйте выбрать более низкое качество.",
        "ID_MESSAGE": "Ваш Telegram ID: {user_id}",
        "UNKNOWN_USER": "неизвестен",
        "ACCESS_START_MESSAGE": "❌ Доступ к боту ограничен.\nВаш Telegram ID: {user_id}\nПопросите владельца бота добавить ваш ID.",
        "ACCESS_MESSAGE": "❌ Доступ к боту ограничен. Узнать свой ID можно командой /id.",
        "ACCESS_ALERT_MESSAGE": "Доступ к боту ограничен.",
        "ACCESS_COMMAND_MESSAGE": "❌ Эта команда доступна только пользователям бота.",
        "HELP_MESSAGE": (
            "Как скачать видео\n"
            "1. Скопируйте ссылку на видео или публикацию.\n"
            "2. Отправьте её мне в личный чат.\n"
            "3. Выберите разрешение видео или MP3.\n"
            "4. Дождитесь готового файла.\n\n"
            "Можно отправить несколько ссылок одним сообщением. Задачи обрабатываются по очереди."
        ),
        "STATUS_ACTIVE_MESSAGE": "⏳ Сейчас обрабатываю видео.",
        "STATUS_IDLE_MESSAGE": "Сейчас ничего не скачиваю.",
        "STATUS_QUEUE_MESSAGE": "В очереди: {count} {task_word}.\n{state}",
        "CANCEL_QUEUE_MESSAGE": "Убрал из очереди: {count} {link_word}",
        "CANCEL_ACTIVE_MESSAGE": "Текущую загрузку остановил",
        "CANCEL_PENDING_MESSAGE": "Отменил выбор формата: {count} {link_word}",
        "CANCEL_EMPTY_MESSAGE": "У вас нет активных или ожидающих задач.",
        "CANCEL_RESULT_MESSAGE": "{result}.",
        "STALE_BUTTON_MESSAGE": "Эта кнопка устарела. Пришлите ссылку ещё раз.",
        "UNKNOWN_FORMAT_MESSAGE": "Неизвестный формат.",
        "STALE_LINK_MESSAGE": "Эта ссылка уже обработана или устарела.",
        "QUEUE_FULL_MESSAGE": "⚠️ Очередь заполнена. Отправьте ссылку ещё раз чуть позже.",
        "RATE_LIMIT_MESSAGE": "Лимит — не более {limit} {link_word} в минуту.",
        "RATE_LIMIT_ONE_LINK": "ссылку",
        "QUEUE_FULL_LINKS_MESSAGE": "⚠️ Очередь сейчас заполнена. Попробуйте отправить оставшиеся ссылки позже.",
        "UNKNOWN_MESSAGE": "Отправьте ссылку на видео, и я подготовлю его для просмотра в Telegram.",
        "JOB_UNSAFE_URL_MESSAGE": "⚠️ Эту ссылку нельзя безопасно открыть. Проверьте её и попробуйте другую.",
        "CANCELLED_MESSAGE": "⚠️ Задача отменена.",
        "MEDIA_CAPTION_MESSAGE": "{title}{quality}\n🌐 {source}",
        "MEDIA_TITLE_VIDEO": "Видео",
        "MEDIA_TITLE_AUDIO": "Аудио",
        "MEDIA_TITLE_GALLERY": "Медиа с сайта",
        "QUALITY_360P": " · 360p",
        "QUALITY_720P": " · 720p",
        "QUALITY_1080P": " · 1080p",
        "QUALITY_MP3": " · MP3",
    }
}


def get_message(key: str, lang: str = "ru", **values: object) -> str:
    """Return a localized message; unknown languages fall back to Russian."""
    template = MESSAGES.get(lang, MESSAGES["ru"])[key]
    return template.format(**values) if values else template


def get_format_buttons(lang: str = "ru") -> dict[str, str]:
    """Return localized labels for the fixed format keyboard."""
    return MESSAGES.get(lang, MESSAGES["ru"])["FORMAT_BUTTONS"]


def get_plural_word(kind: str, count: int, lang: str = "ru") -> str:
    """Return localized Russian link/task count noun."""
    remainder_10, remainder_100 = count % 10, count % 100
    if remainder_10 == 1 and remainder_100 != 11:
        form = "one"
    elif remainder_10 in (2, 3, 4) and remainder_100 not in (12, 13, 14):
        form = "few"
    else:
        form = "many"
    return MESSAGES.get(lang, MESSAGES["ru"])["PLURALS"][kind][form]
