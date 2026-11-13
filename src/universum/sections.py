"""Объём документа: целевые слова, план разделов, склейка и подрезка.

Проблема, которую решает модуль: модель почти никогда не попадает в «5 страниц»
из ползунка — она выдаёт 2 или 12 страниц, а длинный ответ вообще обрывается по
лимиту токенов сервиса (в обрыве остаются незакрытые окружения → Tectonic
падает). Поэтому документ длиннее пары страниц собирается **по разделам**:

1. модель составляет короткий план (список разделов с бюджетами слов);
2. каждый раздел генерируется отдельным небольшим запросом (≈250–350 слов) —
   такой ответ гарантированно доходит целиком и редко содержит ошибки LaTeX;
3. части склеиваются в один документ, объём проверяется и при необходимости
   подрезается.

Здесь же — оценки объёма: сколько слов в теле, сколько это страниц A4 и сколько
разделов нужно на заданный бюджет слов (500/1500/3000 для размеров
«Маленький»/«Средний»/«Большой»).
"""

from __future__ import annotations

import re

__all__ = [
    "WORDS_PER_PAGE",
    "MAX_SECTIONS",
    "MIN_SECTION_WORDS",
    "count_words",
    "estimate_pages",
    "section_count",
    "parse_plan",
    "fallback_plan",
    "split_sections",
    "split_titles",
    "trim_to_words",
    "insert_section",
    "assemble_document",
]


# A4, 11pt, поля 2.2–2.5 см: строка ~13–14 слов, ~45 строк, минус блоки,
# списки и воздух между ними — в среднем ~420 слов на страницу.
WORDS_PER_PAGE = 420
# Сколько разделов максимум генерируем отдельными запросами (иначе слишком долго).
# 24 раздела — это документ на ~30 страниц; каждый ответ ≈400–550 слов, такие
# ответы сервис не обрывает по лимиту токенов.
MAX_SECTIONS = 24
MIN_SECTION_WORDS = 90


_WORD_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
_LST_SUB_RE = re.compile(r"\\begin\{lstlisting\}.*?\\end\{lstlisting\}", re.DOTALL)
_VERB_SUB_RE = re.compile(r"\\verb\S.*?\S")
_DISPLAY_MATH_RE = re.compile(r"\\\[.*?\\\]", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"(?<!\\)\$[^$]{0,400}\$")
_COMMAND_RE = re.compile(r"\\[A-Za-z]+")
_DECOR_RE = re.compile(r"[{}%&_^~$\\|<>*#@]")


def count_words(body: str) -> int:
    """Оценить число слов основного текста в LaTeX-теле.

    Команды, математика и листинги кода не считаются: они не занимают
    «страницы текста» в том смысле, в котором их видит пользователь.

    Args:
        body: Тело документа.

    Returns:
        Число слов (0 для пустого текста).
    """
    text = "" if body is None else str(body)
    if not text.strip():
        return 0
    text = _LST_SUB_RE.sub(" ", text)
    text = _VERB_SUB_RE.sub(" ", text)
    text = _DISPLAY_MATH_RE.sub(" ", text)
    text = _INLINE_MATH_RE.sub(" ", text)
    text = _COMMAND_RE.sub(" ", text)
    text = _DECOR_RE.sub(" ", text)
    return len(_WORD_TOKEN_RE.findall(text))


def estimate_pages(body: str) -> float:
    """Оценить объём тела в страницах A4 (для сверки с ползунком).

    Args:
        body: Тело документа.

    Returns:
        Страницы (дробное число).
    """
    return count_words(body) / WORDS_PER_PAGE


def section_count(words: int) -> int:
    """Сколько разделов нужно, чтобы уложиться в бюджет слов.

    Args:
        words: Целевой объём документа в словах (500/1500/3000 из VolumeSize).

    Returns:
        Число разделов (3…MAX_SECTIONS).
    """
    wanted = round(max(0, int(words)) / 350)  # ~350 слов на раздел — комфортный размер ответа
    return max(3, min(MAX_SECTIONS, wanted))


_HEADING_SPLIT_RE = re.compile(r"(?m)^[ \t]*\\section\*?[ \t]*\{")
_PLAN_LINE_RE = re.compile(r"^[ \t]*(?:\d+[.):]|[-*•—–])[ \t]*(.+?)[ \t]*$")
_PLAN_CUT_RE = re.compile(r"[ \t]*(?:—|–|:|\(|\s-\s)[ \t]*")


def _clean_title(line: str) -> str:
    """Привести строку плана к короткому заголовку раздела."""
    text = line.strip().strip("*_#` ")
    text = re.sub(r"[*_`]", "", text)
    text = _PLAN_CUT_RE.split(text, maxsplit=1)[0]
    text = re.sub(r"\s+", " ", text).strip(" .,;—–-«»\"'")
    if len(text) > 90:
        text = text[:90].rsplit(" ", 1)[0]
    return text


def parse_plan(text: str, *, limit: int = MAX_SECTIONS) -> list[str]:
    """Разобрать ответ модели с планом разделов.

    Понимает «1. Название — пояснение», «- Название», «• Название», а также
    строки без маркера. Мусорные строки («План документа:», пустые) отбрасываются.

    Args:
        text: Ответ LLM с планом.
        limit: Максимум разделов.

    Returns:
        Список названий разделов (может быть пустым).
    """
    if not text:
        return []
    titles: list[str] = []
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line or len(line) < 2:
            continue
        match = _PLAN_LINE_RE.match(line)
        candidate = match.group(1) if match else line
        title = _clean_title(candidate)
        if not title:
            continue
        low = title.lower()
        if low.startswith(("план", "структура документа", "итого", "всего")):
            continue
        if title.endswith((":", "：")):
            continue
        if title not in titles:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


_DEFAULT_PLANS: dict[str, list[str]] = {
    "lecture": ["Введение", "Основные понятия", "Теория и методы", "Примеры и разбор задач", "Выводы"],
    "summary": ["Ключевые тезисы", "Определения", "Формулы", "Примеры", "Итог"],
    "homework": ["Задания базового уровня", "Задания стандартного уровня", "Задания повышенного уровня"],
    "test": [],
}


def fallback_plan(doc_type_value: str, words: int, structure_text: str = "") -> list[str]:
    """План на случай, если модель не вернула пригодный список.

    Args:
        doc_type_value: ``DocType.value`` («lecture» и т.д.).
        words: Целевой объём документа в словах.
        structure_text: Структура, заданная пользователем (приоритетнее).

    Returns:
        Список названий разделов.
    """
    if structure_text.strip():
        lines = [
            re.sub(r"^\s*(?:\d+[.):]|[-*•—–])\s*", "", line).strip()
            for line in structure_text.splitlines()
            if line.strip()
        ]
        titles = [_clean_title(line) for line in lines]
        titles = [t for t in titles if t]
        if titles:
            return titles[:MAX_SECTIONS]
    base = list(_DEFAULT_PLANS.get(doc_type_value, _DEFAULT_PLANS["lecture"]))
    wanted = section_count(words)
    while len(base) < wanted:
        base.insert(len(base) - 1, f"Дополнительный материал {len(base) - 3}")
    return base[: max(3, min(MAX_SECTIONS, wanted))] if wanted >= 3 else base


def split_sections(body: str) -> list[str]:
    """Разбить тело на части по заголовкам верхнего уровня ``\\section``.

    Args:
        body: Тело документа.

    Returns:
        Список кусков: первый может быть «хвостом» до первого раздела.
    """
    text = "" if body is None else str(body)
    if not text.strip():
        return []
    starts = [m.start() for m in _HEADING_SPLIT_RE.finditer(text)]
    if not starts:
        return [text]
    chunks: list[str] = []
    if starts[0] > 0:
        head = text[: starts[0]].strip()
        if head:
            chunks.append(head)
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
    return chunks


_TAIL_RE = re.compile(
    r"вывод|заключени|литератур|источник|итог|критери|answer|conclusion|reference|summary",
    re.IGNORECASE,
)


def trim_to_words(body: str, max_words: int) -> str:
    """Подрезать документ до бюджета, выбрасывая целые разделы.

    Содержание не переписывается: убираются последние разделы (кроме финального
    «Выводы/Литература/Критерии», если он есть), пока объём не уложится в лимит.

    Args:
        body: Тело документа.
        max_words: Максимально допустимое число слов.

    Returns:
        Тело (возможно, с меньшим числом разделов).
    """
    text = "" if body is None else str(body)
    if max_words <= 0 or count_words(text) <= max_words:
        return text
    chunks = split_sections(text)
    if len(chunks) <= 2:
        return text  # резать нечего — лучше целый раздел, чем пустой документ
    tail = chunks[-1] if _TAIL_RE.search(chunks[-1][:400]) else ""
    body_chunks = chunks[:-1] if tail else chunks
    kept: list[str] = []
    used = 0
    for chunk in body_chunks:
        words = count_words(chunk)
        if kept and used + words > max_words:
            break
        kept.append(chunk)
        used += words
    if not kept:
        kept = [body_chunks[0]]
    if tail and count_words(tail) + used <= max_words * 1.15:
        kept.append(tail)
    return "\n\n".join(kept).strip()


_SECTION_TITLE_RE = re.compile(r"(?m)^[ \t]*\\section\*?[ \t]*\{([^{}]*)\}")


def split_titles(body: str) -> list[str]:
    """Вытащить названия разделов из готового тела (для продолжения плана).

    Args:
        body: Тело документа.

    Returns:
        Список названий разделов.
    """
    if not body:
        return []
    return [m.group(1).strip() for m in _SECTION_TITLE_RE.finditer(str(body))]


def insert_section(body: str, chunk: str) -> str:
    """Вставить раздел перед финальным «Выводы/Литература», иначе в конец.

    Args:
        body: Тело документа.
        chunk: Новый раздел.

    Returns:
        Тело с вставленным разделом.
    """
    chunks = split_sections(body)
    if not chunks:
        return chunk.strip()
    if len(chunks) >= 2 and _TAIL_RE.search(chunks[-1][:400]):
        chunks.insert(len(chunks) - 1, chunk.strip())
    else:
        chunks.append(chunk.strip())
    return "\n\n".join(chunks).strip()


def assemble_document(title_latex: str, sections: list[str], *, with_toc: bool = False) -> str:
    """Склеить разделы в тело документа с титулом (и оглавлением).

    Args:
        title_latex: Готовая титульная команда (``\\makelecturetitle{...}``).
        sections: Куски тела по порядку.
        with_toc: Добавить ``\\tableofcontents`` (для длинных документов).

    Returns:
        Тело документа.
    """
    parts = [title_latex.strip()]
    if with_toc:
        parts.append("\\tableofcontents")
    parts.extend(chunk.strip() for chunk in sections if chunk and chunk.strip())
    return "\n\n".join(parts)
