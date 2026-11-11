"""Утилиты: логирование, извлечение LaTeX, дружелюбные ошибки.

Логи пишутся в logs/ без персональных данных (текст пользователя обрезается).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import get_config

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Вернуть настроенный логгер (создаёт logs/universum.log при первом вызове)."""
    global _logger
    if _logger is not None:
        return _logger
    cfg = get_config()
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("universum")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(cfg.logs_dir / "universum.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    _logger = logger
    return logger


def log_event(message: str, *, preview: str = "") -> None:
    """Записать событие в лог (preview — короткий безопасный фрагмент, без PII целиком)."""
    logger = get_logger()
    if preview:
        safe = preview[:120].replace("\n", " ")
        logger.info("%s | preview: %s", message, safe)
    else:
        logger.info(message)


# ---------------------------------------------------------------------------
# Извлечение и проверка LaTeX
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:latex|tex)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_latex(raw: str) -> str:
    """Извлечь LaTeX-код из ответа нейросети.

    Поддерживает ответы в markdown-блоках ```latex ... ``` и чистый код.
    Отрезает пояснения до \\documentclass и после \\end{document}.

    Args:
        raw: Сырой ответ LLM.

    Returns:
        Извлечённый LaTeX-код (может быть пустым, если не найден).
    """
    text = raw.strip()
    if not text:
        return ""
    # Если есть fenced-блоки — взять самый длинный, содержащий documentclass
    fences = _FENCE_RE.findall(text)
    candidates = [f.strip() for f in fences if f.strip()]
    if candidates:
        # Предпочесть блок с \documentclass
        for c in candidates:
            if r"\documentclass" in c:
                text = c
                break
        else:
            text = max(candidates, key=len)
    # Отрезать всё до \documentclass
    idx = text.find(r"\documentclass")
    if idx > 0:
        text = text[idx:]
    # Отрезать всё после \end{document}
    end_idx = text.find(r"\end{document}")
    if end_idx != -1:
        text = text[: end_idx + len(r"\end{document}")]
    return text.strip()


def validate_latex(code: str) -> list[str]:
    """Проверить базовую целостность LaTeX-кода.

    Args:
        code: LaTeX-код.

    Returns:
        Список проблем (пустой = базовая проверка пройдена).
    """
    problems: list[str] = []
    if not code:
        return ["Пустой ответ: LaTeX-код не найден."]
    if r"\documentclass" not in code:
        problems.append("Нет \\documentclass — это не полный LaTeX-документ.")
    if r"\begin{document}" not in code:
        problems.append("Нет \\begin{document}.")
    if r"\end{document}" not in code:
        problems.append("Нет \\end{document}.")
    # Баланс окружений begin/end
    begins = re.findall(r"\\begin\{([^}]+)\}", code)
    ends = re.findall(r"\\end\{([^}]+)\}", code)
    if begins != ends:
        # Простая проверка: множества и порядок могут отличаться из-за вложенности,
        # поэтому сравниваем мультисеты + проверяем незакрытые
        from collections import Counter

        cb, ce = Counter(begins), Counter(ends)
        for env in set(cb) | set(ce):
            if cb[env] != ce[env]:
                problems.append(f"Несбалансированное окружение '{env}': begin={cb[env]}, end={ce[env]}.")
    # Запрещённые пакеты (ломают компиляцию без shell-escape / внешних файлов)
    for pkg in ("minted", "tikz", "pgf", "biblatex", "biber", "glossaries", "cleveref", "siunitx"):
        if re.search(rf"\\usepackage(\[[^\]]*\])?\{{[^}}]*\b{pkg}\b", code):
            problems.append(f"Запрещённый пакет '{pkg}' — удалите его (см. правила промта).")
    if r"\includegraphics" in code:
        problems.append("\\includegraphics без внешних файлов сломает компиляцию — удалите.")
    return problems


# ---------------------------------------------------------------------------
# Дружелюбные ошибки (русский)
# ---------------------------------------------------------------------------

class UniversumError(Exception):
    """Базовая ошибка приложения (показывается пользователю по-русски)."""


class LlmError(UniversumError):
    """Ошибка LLM-клиента."""


class SearchError(UniversumError):
    """Ошибка веб-поиска."""


class FileParseError(UniversumError):
    """Ошибка парсинга файла."""


class LatexCompileError(UniversumError):
    """Ошибка компиляции LaTeX (с логом)."""

    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


FRIENDLY_MESSAGES: dict[str, str] = {
    "no_engine": "Не найден LaTeX-движок. Установите TeX Live / MiKTeX или разрешите авто-скачивание Tectonic (нужен интернет). См. README.",
    "no_network": "Нет соединения с интернетом. Проверьте подключение и попробуйте снова.",
    "llm_failed": "Нейросеть не ответила. Попробуйте ещё раз через минуту — бесплатные сервисы иногда перегружены.",
    "empty_input": "Введите запрос или загрузите файл — иначе не из чего делать документ.",
    "file_too_large": "Файл слишком большой (лимит 20 МБ). Попробуйте файл поменьше.",
    "unsupported_file": "Неподдерживаемый формат файла. Поддерживаются: PDF, DOCX, DOC, TXT, PPTX, изображения.",
    "ocr_unavailable": "Для распознавания изображений нужен Tesseract (https://github.com/tesseract-ocr/tesseract). Установите его или загрузите текстовый файл.",
    "compile_failed": "Не удалось скомпилировать PDF даже после авто-исправлений. Лог ошибки — в окне ниже.",
}
