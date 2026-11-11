"""Конфигурация приложения Universum.

Все параметры собраны в одном месте: эндпоинты LLM, лимиты, стили,
типы документов, пути. Никаких платных ключей не требуется.

Переменные окружения (опционально, для продвинутых пользователей):
    UNIVERSUM_LLM_URL   — свой OpenAI-совместимый эндпоинт (по умолчанию Pollinations).
    UNIVERSUM_LLM_MODEL — модель (по умолчанию openai-fast / gpt-4o-mini на Pollinations).
    UNIVERSUM_LLM_KEY   — ключ только если ваш эндпоинт его требует (по умолчанию не нужен).
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

@dataclass(frozen=True)
class LlmConfig:
    """Настройки LLM-клиента (OpenAI-совместимый API, без ключа по умолчанию)."""

    # Основной эндпоинт: Pollinations.ai — бесплатный, без ключа, OpenAI-совместимый.
    # Документация: https://github.com/pollinations/pollinations/blob/main/APIDOCS.md
    primary_url: str = "https://text.pollinations.ai/openai"
    # Локальный Ollama как fallback (если пользователь его запустил).
    ollama_url: str = "http://localhost:11434/v1/chat/completions"
    ollama_model: str = "llama3.1:8b"
    model: str = field(default_factory=lambda: os.getenv("UNIVERSUM_LLM_MODEL", "openai-fast"))
    api_key: str = field(default_factory=lambda: os.getenv("UNIVERSUM_LLM_KEY", ""))
    custom_url: str = field(default_factory=lambda: os.getenv("UNIVERSUM_LLM_URL", ""))
    timeout_sec: int = 180
    max_retries: int = 3
    retry_backoff_sec: float = 2.0
    max_tokens: int = 8192
    temperature: float = 0.7

    @property
    def endpoints(self) -> list[tuple[str, str]]:
        """Цепочка эндпоинтов (url, model) в порядке приоритета."""
        chain: list[tuple[str, str]] = []
        if self.custom_url:
            chain.append((self.custom_url, self.model))
        chain.append((self.primary_url, self.model))
        chain.append((self.ollama_url, self.ollama_model))
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
