"""Утилиты: логирование, извлечение LaTeX, дружелюбные ошибки.

Логи пишутся в logs/ без персональных данных (текст пользователя обрезается).
"""

from __future__ import annotations

import logging
import re

from .config import get_config
from .latex_sanitizer import sanitize_llm_latex

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
    # Убрать @-макросы ядра (частая причина падения Tectonic)
    return sanitize_latex(text.strip())


_MAKEATLETTER_BLOCK_RE = re.compile(r"\\makeatletter.*?\\makeatother", re.DOTALL)
_MAKEATLETTER_RE = re.compile(r"\\(?:makeatletter|makeatother)\b")
_AT_MACRO_RE = re.compile(r"\\@+")
_SPACEFACTOR_RE = re.compile(r"\\spacefactor\s*\d*")
# \@title / \@author / \@date: подменяем значением из преамбулы, чтобы не
# оставлять в тексте «голое» слово title/author/date
_AT_FIELD_RE = re.compile(r"\\@(title|author|date)\b")
_FIELD_RE = {
    "title": re.compile(r"\\title\s*\{([^{}]*)\}"),
    "author": re.compile(r"\\author\s*\{([^{}]*)\}"),
    "date": re.compile(r"\\date\s*\{([^{}]*)\}"),
}


def sanitize_latex(code: str) -> str:
    """Убрать внутренние макросы LaTeX (@-команды), ломающие компиляцию.

    Модель иногда использует внутренние макросы ядра (``\\@``, ``\\@title``,
    ``\\makeatletter``), из-за чего Tectonic падает с
    ``You can't use \\spacefactor in internal vertical mode``. Промт это
    запрещает, но здесь мы гарантируем результат: такие конструкции
    вырезаются до компиляции.

    Args:
        code: LaTeX-код (может быть None/пустым).

    Returns:
        Очищенный LaTeX-код (всегда строка).
    """
    text = "" if code is None else str(code)
    if not text:
        return ""
    original = text
    removed = 0
    # 0. \@title / \@author / \@date -> значение из преамбулы (или пусто),
    #    иначе после вырезания "\@" остаётся «голое» слово title/author/date.
    fields = {name: (m.group(1) if (m := rx.search(text)) else "") for name, rx in _FIELD_RE.items()}
    text, n = _AT_FIELD_RE.subn(lambda m: fields.get(m.group(1), ""), text)
    removed += n
    # 1. Блоки \makeatletter ... \makeatother целиком (внутри — @-команды)
    text, n = _MAKEATLETTER_BLOCK_RE.subn("", text)
    removed += n
    # 2. Одиночные \makeatletter / \makeatother
    text, n = _MAKEATLETTER_RE.subn("", text)
    removed += n
    # 3. Сами @-команды: \@, \@@ (в т.ч. \@title -> остаётся текст "title")
    text, n = _AT_MACRO_RE.subn("", text)
    removed += n
    # 4. \spacefactor (источник «You can't use \spacefactor…»)
    text, n = _SPACEFACTOR_RE.subn("", text)
    removed += n
    if removed and text != original:
        try:
            get_logger().warning("Removed %d internal @-macro construct(s) from LaTeX", removed)
        except Exception:  # pragma: no cover — логирование не должно ломать генерацию
            pass
    return text


def extract_document_body(raw: str) -> str:
    """Извлечь ТЕЛО LaTeX-документа из ответа нейросети.

    Модель должна вернуть только содержимое между ``\\begin{document}`` и
    ``\\end{document}`` (преамбула задаётся приложением). Но она иногда
    возвращает полный документ или markdown-обёртку — здесь всё приводится
    к телу: снимаются ```-блоки, отрезается преамбула и ``\\end{document}``,
    убирается магическая строка ``% !TEX``.

    Args:
        raw: Сырой ответ LLM (может быть None/пустым).

    Returns:
        Тело документа (может быть пустым, если ничего не найдено).
    """
    text = "" if raw is None else str(raw)
    if not text.strip():
        return ""
    # markdown-обёртки ```latex ... ```
    fences = _FENCE_RE.findall(text)
    candidates = [f.strip() for f in fences if f.strip()]
    if candidates:
        # Предпочитаем блок с \begin{document} или \makelecturetitle
        for c in candidates:
            if r"\begin{document}" in c or r"\makelecturetitle" in c:
                text = c
                break
        else:
            text = max(candidates, key=len)
    # Отрезать преамбулу
    begin = text.find(r"\begin{document}")
    if begin != -1:
        text = text[begin + len(r"\begin{document}"):]
    # Отрезать \end{document} и всё после
    end = text.find(r"\end{document}")
    if end != -1:
        text = text[:end]
    # Убрать магическую строку и \documentclass/\usepackage, если модель их всё же вставила
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("% !TEX"):
            continue
        if stripped.startswith(r"\documentclass") or stripped.startswith(r"\usepackage"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    # Убрать осиротевшие \begin{document}/\end{document} (двойные обёртки)
    text = text.replace(r"\end{document}", "").replace(r"\begin{document}", "")
    text = sanitize_latex(text.strip())
    # Страховка: markdown-мусор, неэкранированные % & # _ ^, незакрытые скобки,
    # окружения и запрещённые команды. Всё это ломает Tectonic ещё до компиляции.
    return sanitize_llm_latex(text)


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
    # tikz/tcolorbox/fontawesome5 разрешены: они уже подключены в преамбуле шаблона
    for pkg in ("minted", "biblatex", "biber", "glossaries", "cleveref", "siunitx"):
        if re.search(rf"\\usepackage(\[[^\]]*\])?\{{[^}}]*\b{pkg}\b", code):
            problems.append(f"Запрещённый пакет '{pkg}' — удалите его (см. правила промта).")
    if r"\includegraphics" in code:
        problems.append("\\includegraphics без внешних файлов сломает компиляцию — удалите.")
    # Внутренние макросы ядра: «You can't use \spacefactor in internal vertical mode»
    if _AT_MACRO_RE.search(code) or _MAKEATLETTER_RE.search(code):
        problems.append("Внутренние @-макросы (\\@, \\makeatletter) ломают компиляцию — удалите их.")
    return problems


# ---------------------------------------------------------------------------
# Титульный лист: тип документа задаёт приложение, данные — только из запроса
# ---------------------------------------------------------------------------

_TITLE_CALL_RE = re.compile(r"\\makelecturetitle\b")
_EXTRA_SPLIT_RE = re.compile(r"\s*[·•|]\s*|\s*;\s*")
_MAX_THEME_CHARS = 90


def latex_to_plain(text: str) -> str:
    """Убрать LaTeX-разметку из короткой строки (для имени файла и проверок).

    Args:
        text: Фрагмент LaTeX (например, тема из ``\\makelecturetitle{...}``).

    Returns:
        Текст без команд, фигурных скобок и математических режимов.
    """
    if not text:
        return ""
    out = str(text)
    # текстовые команды: \textbf{...} -> ...
    out = re.sub(
        r"\\(?:textbf|textit|emph|text|textrm|textsf|texttt|textsc|mbox|hbox|term|hl)\s*\{([^{}]*)\}",
        r"\1",
        out,
    )
    out = re.sub(r"\\([%&$#_{}])", r"\1", out)      # \% -> %, \& -> &
    out = out.replace(r"\\", " ")                   # разрыв строки
    out = re.sub(r"\\[a-zA-Z@]+\s*", "", out)       # остальные команды
    out = out.replace("\\", "")                     # одинокие backslash
    out = re.sub(r"[$^_{}]", "", out)               # математические знаки
    out = out.replace("~", " ").replace("\u00a0", " ")
    return " ".join(out.split())


_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_latex_text(text: str) -> str:
    r"""Экранировать спецсимволы LaTeX в обычном тексте (для титула).

    Нужно потому, что тему/доп. данные мы пересобираем из «плоского» текста:
    без экранирования ``%`` в названии закомментировал бы остаток строки.

    Args:
        text: Обычный текст.

    Returns:
        Текст, безопасный для вставки в LaTeX.
    """
    return "".join(_LATEX_ESCAPES.get(ch, ch) for ch in (text or ""))


# Тип документа в начале темы: «Лекция по физике» → «физике»
_TYPE_PREFIX_RE = re.compile(
    r"^\s*(?:лекци[яиюей]*|конспект\w*|домашн\w*\s+задани\w*|тест\w*|доклад\w*|шпаргалк\w*|реферат\w*"
    r"|lecture\w*|test\w*|summary|notes|homework)\s*"
    r"(?:по|на|об|о|про|about|on|:|—|–|-|№|\.)?\s*",
    re.IGNORECASE,
)


def _strip_type_prefix(text: str) -> str:
    """Убрать тип документа из начала темы (тест/лекция/конспект…)."""
    cleaned = _TYPE_PREFIX_RE.sub("", text or "", count=1)
    return cleaned if cleaned.strip() else (text or "")


def _clean_topic(text: str, limit: int = _MAX_THEME_CHARS) -> str:
    """Привести свободный текст к короткой теме для имени файла."""
    words = (text or "").split()
    if not words:
        return ""
    # Отрезаем служебные слова в начале: «сделай лекцию по теме …»
    while words and words[0].lower().strip(".,:;!?»«\"'") in _REQUEST_FILLERS:
        words.pop(0)
    # Отрезаем служебные обороты в начале темы: «по теме …», «на тему …», «про …»
    while len(words) > 1 and words[0].lower().strip(".,:;!?") in {"по", "на", "про", "о", "об", "для"}:
        words.pop(0)
        if words and words[0].lower().strip(".,:;!?") in {"теме", "тему", "темы"}:
            words.pop(0)
    topic = " ".join(words)
    # Тема в кавычках: «Квантовая механика: основы» — берём то, что внутри
    quoted = re.match(r"^[«\"'\u201c](.+?)[»\"'\u201d]", topic)
    if quoted:
        topic = quoted.group(1)
    # Отрезаем «параметры» в конце: «, 5 страниц», «, с примерами», «, 10 вопросов»
    parts = [part.strip() for part in topic.split(",")]
    kept = [parts[0]]
    for part in parts[1:]:
        if _PARAM_PART_RE.match(part):
            break
        kept.append(part)
    topic = ", ".join(kept)
    topic = topic.strip(" .,:;!?—–-«»\"'")
    if len(topic) > limit:
        cut = topic[:limit].rsplit(" ", 1)[0] or topic[:limit]
        topic = cut.strip(" .,:;!?—–-") + "…"
    return topic


# «Параметры» в конце запроса, которые к теме не относятся
_PARAM_PART_RE = re.compile(
    r"^(?:\d+\s*)?(?:страниц|вопрос|вариант|слайд|слов|пример|задач|пункт|минут|источник)"
    r"|^с\s+[а-яa-z]|^объ[её]м|^пожалуйста|^page|^with\s",
    re.IGNORECASE,
)

# Служебные слова, которые в начале запроса не относятся к теме
_REQUEST_FILLERS = {
    "сделай", "сделайте", "создай", "создайте", "подготовь", "подготовьте",
    "напиши", "напишите", "составь", "составьте", "сгенерируй", "сгенерируйте",
    "нужен", "нужна", "нужно", "нужны", "хочу", "хотелось", "пожалуйста",
    "make", "create", "write", "generate", "please", "need", "prepare",
    "about", "on", "for", "regarding", "the", "a", "an",
    # тип документа в винительном/именительном падеже
    "лекцию", "лекция", "лекции", "конспект", "конспекта", "тест", "теста",
    "тесты", "доклад", "доклада", "шпаргалку", "шпаргалка", "реферат", "реферата",
    "домашнее", "домашнюю", "задание", "задания", "домашку", "lecture", "test",
    "summary", "notes", "homework",
}


def topic_from_request(text: str, limit: int = _MAX_THEME_CHARS) -> str:
    """Вытащить короткую тему из свободного запроса пользователя.

    Args:
        text: Текст запроса.
        limit: Максимальная длина темы в символах.

    Returns:
        Тема (пустая строка, если запрос пустой).
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    # Первая содержательная строка и первое предложение — обычно именно тема
    first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    first_sentence = re.split(r"(?<=[.!?])\s+", first_line, maxsplit=1)[0]
    sentence = first_sentence.strip() or first_line
    # Длинный перечень пожеланий через запятую отрезаем: берём первую часть
    sentence = re.split(r",\s*(?=[а-яА-Яa-zA-Z]{2,})", sentence, maxsplit=1)[0] if len(sentence) > limit else sentence
    return _clean_topic(latex_to_plain(sentence), limit=limit)


def theme_from_latex(latex_code: str) -> str:
    """Извлечь тему из ``\\makelecturetitle`` (для имени файла).

    Args:
        latex_code: Тело или полный LaTeX-документ.

    Returns:
        Тема без разметки (пустая строка, если команды нет).
    """
    if not latex_code:
        return ""
    call = _parse_title_call(latex_code)
    if call is None:
        return ""
    args = call["args"]  # type: ignore[index]
    return _clean_topic(latex_to_plain(str(args[-1])))


# --- разрывы страниц в тестах -----------------------------------------------

_HEADING_RE = re.compile(r"^[ \t]*\\(?:section|subsection)\*?\s*\{([^{}]*)\}[ \t]*$", re.MULTILINE)
_VARIANT_TITLE_RE = re.compile(
    r"^\s*(?:вариант|variant|option|вариант\s*№)\s*[№#]?\s*\d+", re.IGNORECASE
)
_KEY_TITLE_RE = re.compile(
    r"^\s*(?:ключ\w*\s+ответ\w*|ответ\w*\s+к\s+тест\w*|answer\s*key|answers|ключи)\b",
    re.IGNORECASE,
)
_PAGE_BREAK_TAIL_RE = re.compile(r"\\(?:clearpage|newpage|pagebreak)\s*$")


def _heading_kind(title: str) -> str:
    """Тип заголовка: 'variant', 'key' или ''."""
    if _VARIANT_TITLE_RE.match(title or ""):
        return "variant"
    if _KEY_TITLE_RE.match(title or ""):
        return "key"
    return ""


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 вариант, 2 варианта, 5 вариантов."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def count_variants(body: str) -> int:
    """Сколько вариантов теста в теле документа (по заголовкам).

    Args:
        body: Тело документа.

    Returns:
        Число заголовков вида «Вариант 1», «Variant 2», «Option 3».
    """
    if not body:
        return 0
    return sum(
        1
        for match in _HEADING_RE.finditer(str(body))
        if _heading_kind(match.group(1)) == "variant"
    )


def ensure_page_breaks(body: str) -> str:
    r"""Гарантировать ``\clearpage`` перед каждым вариантом теста и ключом ответов.

    Требование UX: каждый вариант начинается с новой страницы, а ключ ответов —
    отдельный раздел с новой страницы (чтобы не смешиваться с вопросами). Промт
    это требует, но модель может забыть — поэтому разрывы страниц расставляются
    детерминированно.

    Args:
        body: Тело документа от модели.

    Returns:
        Тело, где перед заголовками «Вариант N» и «Ключ ответов» стоит
        ``\clearpage`` (если его там ещё нет).
    """
    text = "" if body is None else str(body)
    if not text:
        return ""
    result: list[str] = []
    last = 0
    for match in _HEADING_RE.finditer(text):
        if _heading_kind(match.group(1)) not in {"variant", "key"}:
            continue
        before = text[last:match.start()]
        stripped = before.rstrip()
        if stripped and not _PAGE_BREAK_TAIL_RE.search(stripped):
            result.append(before)
            result.append("\n\\clearpage\n")  # новая страница перед заголовком
        else:
            result.append(before)
        result.append(text[match.start():match.end()])
        last = match.end()
    result.append(text[last:])
    out = "".join(result)
    return out


# --- имена файлов -----------------------------------------------------------

_FILENAME_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, *, max_length: int = 120, fallback: str = "Universum") -> str:
    """Сделать безопасное имя файла для Windows/macOS/Linux.

    Убирает запрещённые символы (``\\ / : * ? " < > |`` и управляющие),
    схлопывает пробелы, обрезает длину и защищает зарезервированные имена
    (``CON``, ``NUL``, ``COM1``…), которые Windows не принимает.

    Args:
        name: Желаемое имя (без пути).
        max_length: Максимальная длина имени в символах.
        fallback: Имя на случай, если после очистки ничего не осталось.

    Returns:
        Безопасное имя файла.
    """
    text = (name or "").replace("\u00a0", " ")
    text = text.replace(":", " —")          # двоеточие в Windows запрещено
    text = _FILENAME_FORBIDDEN.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[—–]{2,}", "—", text)
    text = re.sub(r"\s+([—–])", r" \1", text)
    text = text.strip(" .")
    # Точки в конце Windows тоже не любит
    text = text.rstrip(" .")
    if len(text) > max_length:
        cut = text[:max_length].rsplit(" ", 1)[0] or text[:max_length]
        text = cut.rstrip(" .—–-")
    if not text:
        return fallback
    stem = text.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        text = f"_{text}"
    return text


def suggest_document_name(
    doc_type_label: str,
    request_text: str = "",
    latex_code: str = "",
    extension: str = ".pdf",
) -> str:
    """Собрать имя PDF вида «Лекция — Налогообложение в России».

    Тема берётся из титульного листа сгенерированного документа (самый точный
    источник), иначе — из запроса пользователя.

    Args:
        doc_type_label: Название типа документа по-русски («Лекция», «Тест»…).
        request_text: Запрос пользователя.
        latex_code: Сгенерированный LaTeX (тело или полный документ).
        extension: Расширение файла (по умолчанию ``.pdf``).

    Returns:
        Безопасное имя файла с расширением.
    """
    theme = theme_from_latex(latex_code) or topic_from_request(request_text) or "Документ"
    label = (doc_type_label or "").strip()
    base = f"{label} — {theme}" if label else theme
    if label and theme.lower().startswith(label.lower()):
        base = theme  # «Тест — Тест по физике» не нужно
    return sanitize_filename(base) + extension


# --- титульная команда ------------------------------------------------------

def _in_comment(text: str, position: int) -> bool:
    """Стоит ли позиция внутри LaTeX-комментария (после незаэкранированного %)."""
    line_start = text.rfind("\n", 0, position) + 1
    prefix = text[line_start:position]
    escaped = False
    for ch in prefix:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "%":
            return True
    return False


def _parse_title_call(text: str) -> dict[str, object] | None:
    r"""Разобрать первое «настоящее» ``\makelecturetitle[доп]{…}`` в тексте.

    Поддерживается и старый 4-аргументный вид (``{Курс}{Семестр}{Тип}{Тема}``),
    и новый (``[доп]{Тема}``) — модели часто путают форматы. Комментарии
    (``% ...``) и определения в преамбуле (``\newcommand{\makelecturetitle}``)
    пропускаются: они не создают титул.

    Returns:
        ``{"start", "end", "optional", "args"}`` или ``None``.
    """
    if not text:
        return None
    for match in _TITLE_CALL_RE.finditer(text):
        if _in_comment(text, match.start()):
            continue
        pos = match.end()
        optional = ""
        k = pos
        while k < len(text) and text[k] in " \t\r\n":
            k += 1
        if k < len(text) and text[k] == "[":
            close = text.find("]", k)
            if close != -1:
                optional = text[k + 1: close]
                pos = close + 1
        args: list[str] = []
        end_pos = pos
        k = pos
        while True:
            while k < len(text) and text[k] in " \t\r\n":
                k += 1
            if k >= len(text) or text[k] != "{":
                break
            depth = 0
            start = k
            while k < len(text):
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                    if depth == 0:
                        k += 1
                        break
                k += 1
            args.append(text[start + 1: k - 1])
            end_pos = k  # конец последнего аргумента, без «съеденных» пробелов
        if not args:
            continue  # \newcommand{\makelecturetitle}[2][] — не вызов
        return {"start": match.start(), "end": end_pos, "optional": optional, "args": args}
    return None


def normalize_title_call(body: str, doc_type_label: str = "", request_text: str = "") -> str:
    """Привести титул к безопасному виду: тема + только явные доп. данные.

    Модель любит добавлять «Семестр 2», «Модуль 1», «Лектор: Фамилия И.О.» и путать
    тип документа. Здесь:
      * тема — последний аргумент команды (или первый, если аргумент один);
      * всё остальное (прежние «курс/семестр/тип», содержимое ``[…]``) остаётся
        только если эти слова реально есть в запросе пользователя;
      * название типа документа модель не задаёт вовсе — его подставляет
        приложение через ``\\universumdoctype``.

    Args:
        body: Тело документа от модели.
        doc_type_label: Название выбранного типа («Тест»), чтобы выкинуть его из
            доп. данных.
        request_text: Запрос пользователя (источник явных данных).

    Returns:
        Тело с нормализованной (или добавленной) титульной командой.
    """
    text = "" if body is None else str(body)
    parsed = _parse_title_call(text)
    request_lower = " ".join((request_text or "").lower().split())

    def _explicit(fragment: str) -> bool:
        """Фрагмент пришёл из запроса пользователя, а не выдумка модели."""
        cleaned = latex_to_plain(fragment).lower().strip(" .:;—–-")
        if not cleaned or not request_lower:
            return False
        if doc_type_label and cleaned == doc_type_label.lower():
            return False
        return cleaned in request_lower

    if parsed is None:
        theme = _strip_type_prefix(
            _clean_topic(latex_to_plain(topic_from_request(request_text)))
        ) or "Документ"
        return r"\makelecturetitle{" + escape_latex_text(theme) + "}\n" + text.lstrip("\n")

    args = [str(a) for a in parsed["args"]]  # type: ignore[index]
    optional = str(parsed["optional"])
    theme_plain = _strip_type_prefix(_clean_topic(latex_to_plain(args[-1])))
    if not theme_plain:
        theme_plain = _clean_topic(latex_to_plain(topic_from_request(request_text)))
    theme_plain = theme_plain or "Документ"

    extras: list[str] = []
    candidates: list[str] = []
    if optional.strip():
        candidates.append(optional)
    candidates.extend(args[:-1])
    for candidate in candidates:
        for fragment in _EXTRA_SPLIT_RE.split(candidate):
            fragment = fragment.strip(" .;")
            if not fragment:
                continue
            # явно выдуманные типы документов не показываем — тип ставит приложение
            plain = latex_to_plain(fragment).lower()
            if doc_type_label and plain.startswith(doc_type_label.lower()):
                continue
            if _explicit(fragment) and fragment not in extras:
                extras.append(fragment)

    new_call = r"\makelecturetitle"
    if extras:
        new_call += "[" + " · ".join(escape_latex_text(item) for item in extras) + "]"
    new_call += "{" + escape_latex_text(theme_plain) + "}"
    start, end = int(parsed["start"]), int(parsed["end"])  # type: ignore[index]
    return text[:start] + new_call + text[end:]


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
