"""Конфигурация приложения Universum.

Все параметры собраны в одном месте: эндпоинты LLM, лимиты, стили,
типы документов, пути.

По умолчанию ключи не требуются (Pollinations). Если у вас есть ключ
провайдера (OpenAI/OpenRouter/DeepSeek и т.п.), его можно указать:
- через GUI «Настройки нейросети» (сохраняется в OS keyring или ~/.universum/);
- или через переменные окружения UNIVERSUM_LLM_URL / UNIVERSUM_LLM_MODEL / UNIVERSUM_LLM_KEY.

Ключ никогда не пишется в репозиторий и не логируется.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------

def project_root() -> Path:
    """Корень проекта (родитель папки src/)."""
    return Path(__file__).resolve().parents[2]


def default_logs_dir() -> Path:
    """Папка логов (создаётся при первом запуске)."""
    return project_root() / "logs"


def default_temp_dir() -> Path:
    """Временная папка для LaTeX-компиляции."""
    import tempfile

    return Path(tempfile.gettempdir()) / "universum"


# ---------------------------------------------------------------------------
# Перечисления
# ---------------------------------------------------------------------------

class DocType(str, Enum):
    """Тип учебного документа."""

    LECTURE = "lecture"
    SUMMARY = "summary"  # конспект
    HOMEWORK = "homework"
    TEST = "test"

    @property
    def label_ru(self) -> str:
        return {
            DocType.LECTURE: "Лекция",
            DocType.SUMMARY: "Конспект",
            DocType.HOMEWORK: "Домашнее задание",
            DocType.TEST: "Тест",
        }[self]


class Style(str, Enum):
    """Стиль оформления."""

    STRICT = "strict"
    COLORFUL = "colorful"
    PLAIN = "plain"

    @property
    def label_ru(self) -> str:
        return {
            Style.STRICT: "Строгий",
            Style.COLORFUL: "Красочный / креативный",
            Style.PLAIN: "Без стиля",
        }[self]


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

class LlmProvider(str, Enum):
    """Провайдер нейросети (OpenAI-совместимый API)."""

    POLLINATIONS = "pollinations"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    DEEPSEEK = "deepseek"
    OLLAMA = "ollama"
    CUSTOM = "custom"

    @property
    def label_ru(self) -> str:
        return {
            LlmProvider.POLLINATIONS: "Pollinations (бесплатно, без ключа)",
            LlmProvider.OPENAI: "OpenAI (нужен ключ)",
            LlmProvider.OPENROUTER: "OpenRouter (нужен ключ)",
            LlmProvider.DEEPSEEK: "DeepSeek (нужен ключ)",
            LlmProvider.OLLAMA: "Ollama локально (без ключа)",
            LlmProvider.CUSTOM: "Свой эндпоинт (OpenAI-совместимый)",
        }[self]


# Пресеты провайдеров: url, модель по умолчанию, нужен ли ключ
# Pollinations: актуальный эндпоинт gen.pollinations.ai (старый text.pollinations.ai
# отвечает 400 output_token_limit_exceeded при max_tokens=8192).
POLLINATIONS_URL = "https://gen.pollinations.ai/v1/chat/completions"
POLLINATIONS_MODEL = "openai"
OLD_POLLINATIONS_URL = "https://text.pollinations.ai/openai"
OLD_POLLINATIONS_MODEL = "openai-fast"

PROVIDER_PRESETS: dict[LlmProvider, dict[str, object]] = {
    LlmProvider.POLLINATIONS: {
        "url": POLLINATIONS_URL,
        "model": POLLINATIONS_MODEL,
        "needs_key": False,
    },
    LlmProvider.OPENAI: {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "needs_key": True,
    },
    LlmProvider.OPENROUTER: {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "openai/gpt-4o-mini",
        "needs_key": True,
    },
    LlmProvider.DEEPSEEK: {
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "needs_key": True,
    },
    LlmProvider.OLLAMA: {
        "url": "http://localhost:11434/v1/chat/completions",
        "model": "llama3.1:8b",
        "needs_key": False,
    },
    LlmProvider.CUSTOM: {
        "url": "",
        "model": "",
        "needs_key": True,
    },
}


def mask_key(key: str) -> str:
    """Замаскировать ключ для логов/GUI: 'sk-...abcd' (никогда не показывать целиком)."""
    if not key:
        return "(не задан)"
    k = key.strip()
    if len(k) <= 8:
        return "***"
    return f"{k[:3]}...{k[-4:]}"


def is_localhost_url(url: str) -> bool:
    """Проверить, что URL указывает на localhost (ключ туда не отправляем)."""
    u = (url or "").lower()
    return "localhost" in u or "127.0.0.1" in u or "[::1]" in u


def migrate_pollinations_url(url: str) -> str:
    """Мигрировать старый URL Pollinations на новый gen.pollinations.ai."""
    u = (url or "").strip()
    if "text.pollinations.ai" in u:
        return POLLINATIONS_URL
    return u


def migrate_pollinations_model(model: str) -> str:
    """Мигрировать старую модель Pollinations на актуальную."""
    m = (model or "").strip()
    if m == OLD_POLLINATIONS_MODEL:
        return POLLINATIONS_MODEL
    return m


def is_pollinations_url(url: str) -> bool:
    """Проверить, что URL — Pollinations (для него max_tokens не отправляем)."""
    return "pollinations.ai" in (url or "").lower()


def settings_dir() -> Path:
    """Папка пользовательских настроек (~/.universum)."""
    return Path.home() / ".universum"


@dataclass
class LlmSettings:
    """Настройки нейросети, сохраняемые пользователем (GUI)."""

    provider: LlmProvider = LlmProvider.POLLINATIONS
    url: str = ""
    model: str = ""
    api_key: str = ""

    def resolved_url(self) -> str:
        """URL с учётом пресета провайдера (старый Pollinations мигрируется)."""
        if self.url.strip():
            return migrate_pollinations_url(self.url.strip())
        preset = PROVIDER_PRESETS.get(self.provider, {})
        return migrate_pollinations_url(str(preset.get("url", "")))

    def resolved_model(self) -> str:
        """Модель с учётом пресета провайдера (старая Pollinations мигрируется)."""
        if self.model.strip():
            return migrate_pollinations_model(self.model.strip())
        preset = PROVIDER_PRESETS.get(self.provider, {})
        return migrate_pollinations_model(str(preset.get("model", "")))

    def __repr__(self) -> str:  # ключ никогда не попадает в repr/логи
        return f"LlmSettings(provider={self.provider.value}, url={self.resolved_url()!r}, model={self.resolved_model()!r}, api_key={mask_key(self.api_key)})"


def load_llm_settings() -> LlmSettings:
    """Загрузить настройки: env > keyring > файл ~/.universum/settings.json > по умолчанию."""
    # 1. Переменные окружения имеют наивысший приоритет
    env_url = os.getenv("UNIVERSUM_LLM_URL", "").strip()
    env_model = os.getenv("UNIVERSUM_LLM_MODEL", "").strip()
    env_key = os.getenv("UNIVERSUM_LLM_KEY", "").strip()
    if env_url or env_model or env_key:
        provider = LlmProvider.CUSTOM if env_url else LlmProvider.POLLINATIONS
        return LlmSettings(provider=provider, url=env_url, model=env_model, api_key=env_key)
    # 2. OS keyring (если установлен пакет keyring)
    try:
        import keyring  # type: ignore

        key = keyring.get_password("universum", "llm_api_key") or ""
        if key:
            # URL/модель берём из файла, ключ — из keyring
            file_settings = _load_settings_file()
            file_settings.api_key = key
            return file_settings
    except Exception:
        pass
    # 3. Файл настроек
    return _load_settings_file()


def _load_settings_file() -> LlmSettings:
    """Прочитать ~/.universum/settings.json (без ключа, если он в keyring)."""
    import json

    path = settings_dir() / "settings.json"
    if not path.exists():
        return LlmSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        provider = LlmProvider(data.get("provider", "pollinations"))
        return LlmSettings(
            provider=provider,
            url=str(data.get("url", "")),
            model=str(data.get("model", "")),
            api_key=str(data.get("api_key", "")),
        )
    except Exception:
        return LlmSettings()


def save_llm_settings(settings: LlmSettings) -> None:
    """Сохранить настройки. Ключ — в keyring (если доступен), иначе в файл с правами 0600.

    Args:
        settings: Настройки для сохранения.
    """
    import json

    settings_dir().mkdir(parents=True, exist_ok=True)
    path = settings_dir() / "settings.json"
    saved_key = ""
    # Пробуем keyring
    key_saved_to_keyring = False
    if settings.api_key.strip():
        try:
            import keyring  # type: ignore

            keyring.set_password("universum", "llm_api_key", settings.api_key.strip())
            key_saved_to_keyring = True
        except Exception:
            key_saved_to_keyring = False
    if not key_saved_to_keyring:
        saved_key = settings.api_key.strip()
    data = {
        "provider": settings.provider.value,
        "url": settings.url.strip(),
        "model": settings.model.strip(),
        "api_key": saved_key,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Ограничить права (на Windows — best effort)
    try:
        path.chmod(0o600)
    except Exception:
        pass


@dataclass(frozen=True)
class LlmConfig:
    """Настройки LLM-клиента (OpenAI-совместимый API).

    Pollinations: строго https://gen.pollinations.ai/v1/chat/completions,
    модель openai, max_tokens НЕ отправляется (сервер сам решает лимит —
    иначе 400 output_token_limit_exceeded).
    """

    primary_url: str = POLLINATIONS_URL
    ollama_url: str = "http://localhost:11434/v1/chat/completions"
    ollama_model: str = "llama3.1:8b"
    model: str = field(default_factory=lambda: migrate_pollinations_model(os.getenv("UNIVERSUM_LLM_MODEL", POLLINATIONS_MODEL)))
    api_key: str = field(default_factory=lambda: os.getenv("UNIVERSUM_LLM_KEY", ""))
    custom_url: str = field(default_factory=lambda: migrate_pollinations_url(os.getenv("UNIVERSUM_LLM_URL", "")))
    provider: LlmProvider = LlmProvider.POLLINATIONS
    timeout_sec: int = 180
    max_retries: int = 3
    retry_backoff_sec: float = 2.0
    # None = не отправлять max_tokens (сервер решает). Для Pollinations — всегда None.
    max_tokens: int | None = None
    temperature: float = 0.7

    @classmethod
    def from_settings(cls, settings: LlmSettings) -> "LlmConfig":
        """Создать конфиг из пользовательских настроек (GUI)."""
        url = settings.resolved_url()
        model = settings.resolved_model()
        # Если провайдер Pollinations/Ollama — это primary, иначе custom
        if settings.provider in (LlmProvider.POLLINATIONS, LlmProvider.OLLAMA):
            return cls(
                primary_url=url or POLLINATIONS_URL,
                model=model or POLLINATIONS_MODEL,
                api_key=settings.api_key.strip(),
                provider=settings.provider,
            )
        return cls(
            primary_url=POLLINATIONS_URL,
            custom_url=url,
            model=model,
            api_key=settings.api_key.strip(),
            provider=settings.provider,
        )

    @property
    def endpoints(self) -> list[tuple[str, str, bool]]:
        """Цепочка эндпоинтов (url, model, send_key) в порядке приоритета.

        send_key=True только для удалённых эндпоинтов с ключом (не для localhost).
        """
        chain: list[tuple[str, str, bool]] = []
        has_key = bool(self.api_key.strip())
        if self.custom_url:
            chain.append((self.custom_url, self.model, has_key and not is_localhost_url(self.custom_url)))
        # Primary: ключ шлём только если провайдер требует и это не localhost
        primary_send_key = has_key and self.provider not in (LlmProvider.POLLINATIONS, LlmProvider.OLLAMA) and not is_localhost_url(self.primary_url)
        # Если primary — Pollinations без ключа, ключ не нужен
        if self.provider == LlmProvider.POLLINATIONS:
            primary_send_key = False
        chain.append((self.primary_url, self.model, primary_send_key))
        # Ollama всегда без ключа
        chain.append((self.ollama_url, self.ollama_model, False))
        return chain


# ---------------------------------------------------------------------------
# Веб-поиск
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchConfig:
    """Настройки веб-поиска (DuckDuckGo, без ключа)."""

    max_results: int = 8
    timeout_sec: int = 30
    max_retries: int = 2


# ---------------------------------------------------------------------------
# LaTeX
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LatexConfig:
    """Настройки компиляции LaTeX."""

    max_repair_attempts: int = 3
    compile_timeout_sec: int = 120
    # Порядок поиска движков: tectonic (один бинарь, авто-скачивание) -> xelatex -> pdflatex -> lualatex
    engine_preference: tuple[str, ...] = ("tectonic", "xelatex", "pdflatex", "lualatex")
    # URL релизов Tectonic на GitHub (бесплатно, без ключа)
    tectonic_release_base: str = "https://github.com/tectonic-typesetting/tectonic/releases"


# ---------------------------------------------------------------------------
# Файлы и лимиты
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Limits:
    """Лимиты безопасности и производительности."""

    max_upload_bytes: int = 20 * 1024 * 1024  # 20 МБ
    max_extracted_chars: int = 60_000  # сколько текста отдаём в промт
    max_request_chars: int = 20_000
    max_structure_chars: int = 8_000
    allowed_extensions: tuple[str, ...] = (
        ".pdf", ".docx", ".doc", ".txt", ".pptx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
    )


# ---------------------------------------------------------------------------
# Объём документа
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Volume:
    """Объём документа: страницы (лекция/конспект/ДЗ) или вопросы (тест)."""

    pages: int = 5
    questions: int = 10
    variants: int = 4

    def __post_init__(self) -> None:
        if not (1 <= self.pages <= 30):
            raise ValueError("pages должен быть от 1 до 30")
        if not (1 <= self.questions <= 50):
            raise ValueError("questions должен быть от 1 до 50")
        if not (2 <= self.variants <= 6):
            raise ValueError("variants должен быть от 2 до 6")


# ---------------------------------------------------------------------------
# Главная конфигурация
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    """Единая конфигурация приложения."""

    llm: LlmConfig = field(default_factory=LlmConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    latex: LatexConfig = field(default_factory=LatexConfig)
    limits: Limits = field(default_factory=Limits)
    logs_dir: Path = field(default_factory=default_logs_dir)
    temp_dir: Path = field(default_factory=default_temp_dir)


def get_config() -> AppConfig:
    """Создать конфигурацию по умолчанию (с учётом env-переменных)."""
    return AppConfig()
