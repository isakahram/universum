"""Промты для нейросети: генерация LaTeX и авто-ремонт.

Модуль содержит:
- системный промт (роль, жёсткие правила компилируемости);
- блоки стилей (3): строгий / красочный / без стиля;
- блоки типов документов (4): лекция / конспект / ДЗ / тест;
- промт авто-ремонта LaTeX;
- функцию сборки полного промта :func:`build_generation_prompt`.

Все промты требуют от модели вернуть ПОЛНЫЙ компилируемый LaTeX-документ.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import DocType, Style, Volume

# ---------------------------------------------------------------------------
# Системный промт
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Ты — опытный преподаватель и эксперт по LaTeX. Твоя задача — создать учебный документ в виде ПОЛНОГО компилируемого LaTeX-документа.

ЖЁСТКИЕ ПРАВИЛА (нарушение = брак):
1. Верни ТОЛЬКО код LaTeX. Никаких пояснений до/после. Допустимо обернуть код в ```latex ... ```.
2. Документ должен быть полным: \\documentclass ... \\begin{document} ... \\end{document}.
3. Используй ТОЛЬКО разрешённые пакеты (см. блок стиля). ЗАПРЕЩЕНО: minted, tikz/pgf, biber/biblatex, glossaries, cleveref, siunitx, physics, chemformula и любые другие пакеты вне списка.
4. Для листингов кода используй ТОЛЬКО listings (окружение lstlisting), НЕ minted. Указывай language (Python, Java, C, SQL и т.д.).
5. Математика: $...$ для формул в строке, \\[...\\] / equation / align для выносных. Проверяй баланс скобок и окружений.
6. Кириллица: обязательно \\usepackage[T1]{fontenc}, \\usepackage[utf8]{inputenc}, \\usepackage[russian,english]{babel} (порядок: russian первым, если документ на русском).
7. Экранируй спецсимволы в тексте: % & $ # _ { } ~ ^ \\ . Внутри формул и листингов — не экранируй.
8. Не используй \\input, \\include, \\includegraphics с внешними файлами (картинок нет). Таблицы — только tabular/tabularx/booktabs.
9. Ссылки: только \\label/\\ref внутри документа и \\url{...} для внешних URL. Не выдумывай DOI.
10. Язык документа = язык запроса пользователя (русский или английский). Заголовки, подписи, оглавление — на этом языке.
11. Не оставляй плейсхолдеров вида [вставить сюда]. Каждый раздел должен содержать реальный содержательный текст.
12. Проверь перед ответом: все \\begin{...} закрыты \\end{...}, нет незакрытых скобок, преамбула перед \\begin{document}.
"""

# ---------------------------------------------------------------------------
# Блоки стилей
# ---------------------------------------------------------------------------

STYLE_STRICT = """СТИЛЬ: СТРОГИЙ (академический).
Преамбула (используй ровно этот набор, можно добавить только geometry/hyperref параметры):
\\documentclass[11pt,a4paper]{article}
\\usepackage[T1]{fontenc}
\\usepackage[utf8]{inputenc}
\\usepackage[russian,english]{babel}
\\usepackage{lmodern}
\\usepackage[margin=2.2cm]{geometry}
\\usepackage{amsmath,amssymb}
\\usepackage{graphicx}
\\usepackage{booktabs}
\\usepackage{tabularx}
\\usepackage{enumitem}
\\usepackage{titlesec}
\\usepackage{fancyhdr}
\\usepackage{microtype}
\\usepackage{listings}
\\usepackage{xcolor}
\\usepackage[colorlinks=true,linkcolor=black,urlcolor=black,citecolor=black]{hyperref}
\\usepackage{caption}
\\usepackage{float}
\\usepackage{ragged2e}
\\lstset{basicstyle=\\ttfamily\\small,breaklines=true,frame=single,numbers=left,numberstyle=\\tiny,tabsize=4,showstringspaces=false}
Оформление: чёрно-белая гамма, serif-шрифт, строгая нумерация разделов (\\section, \\subsection), титульный лист, оглавление (\\tableofcontents), колонтитулы с номером страницы, аккуратные таблицы booktabs. Никаких цветных блоков и рамок tcolorbox. Заголовки — чёрные, без декоративные элементы."""

STYLE_COLORFUL = """СТИЛЬ: КРАСОЧНЫЙ / КРЕАТИВНЫЙ.
Преамбула (используй ровно этот набор):
\\documentclass[11pt,a4paper]{article}
\\usepackage[T1]{fontenc}
\\usepackage[utf8]{inputenc}
\\usepackage[russian,english]{babel}
\\usepackage{lmodern}
\\usepackage[margin=2.2cm]{geometry}
\\usepackage{amsmath,amssymb}
\\usepackage{graphicx}
\\usepackage{booktabs}
\\usepackage{tabularx}
\\usepackage{enumitem}
\\usepackage{titlesec}
\\usepackage{fancyhdr}
\\usepackage{microtype}
\\usepackage{listings}
\\usepackage[table]{xcolor}
\\usepackage{tcolorbox}
\\usepackage[colorlinks=true,linkcolor=blue!60!black,urlcolor=blue!60!black,citecolor=blue!60!black]{hyperref}
\\usepackage{caption}
\\usepackage{float}
\\usepackage{ragged2e}
\\definecolor{maincolor}{RGB}{30,90,160}
\\definecolor{accentcolor}{RGB}{230,126,34}
\\definecolor{lightbg}{RGB}{235,243,252}
\\lstset{basicstyle=\\ttfamily\\small,breaklines=true,frame=single,numbers=left,numberstyle=\\tiny,tabsize=4,showstringspaces=false,backgroundcolor=\\color{lightbg}}
Оформление: цветные акценты (maincolor/accentcolor), ключевые определения и выводы — в tcolorbox (colback=lightbg, colframe=maincolor), заголовки разделов — цветные (\\titleformat с \\color{maincolor}), аккуратные цветные блоки, но без перегруза. Титульный лист с цветной плашкой, оглавление. Сохраняй читаемость: цвет — акцент, а не фон всего текста."""

STYLE_PLAIN = """СТИЛЬ: БЕЗ СТИЛЯ (минимум оформления).
Преамбула (используй ровно этот набор, ничего лишнего):
\\documentclass[11pt,a4paper]{article}
\\usepackage[T1]{fontenc}
\\usepackage[utf8]{inputenc}
\\usepackage[russian,english]{babel}
\\usepackage{lmodern}
\\usepackage[margin=2.5cm]{geometry}
\\usepackage{amsmath,amssymb}
\\usepackage{listings}
\\usepackage{hyperref}
\\lstset{basicstyle=\\ttfamily\\small,breaklines=true,frame=single,tabsize=4,showstringspaces=false}
Оформление: стандартный LaTeX без цветов, без tcolorbox, без fancyhdr, без titlesec. Простые \\section/\\subsection, стандартные списки, без титульного листа с декором (допустим простой \\title/\\maketitle или просто заголовок). Никаких цветных команд."""

STYLE_BLOCKS: dict[Style, str] = {
    Style.STRICT: STYLE_STRICT,
    Style.COLORFUL: STYLE_COLORFUL,
    Style.PLAIN: STYLE_PLAIN,
}

# ---------------------------------------------------------------------------
# Блоки типов документов
# ---------------------------------------------------------------------------

DOCTYPE_LECTURE = """ТИП ДОКУМЕНТА: ЛЕКЦИЯ.
Структура (строго в этом порядке):
1. Титульный лист: название темы (\\title), подзаголовок, дата, \\maketitle + \\thispagestyle{empty} где уместно.
2. \\tableofcontents (если документ длинный).
3. Введение (\\section*{Введение} или \\section{Введение}): актуальность темы, цели и задачи лекции, что студент будет знать/уметь после.
4. Основные разделы: 3–5 логических разделов (или по структуре пользователя, если она задана). Каждый раздел: теория + примеры/кейсы + формулы/таблицы где уместно.
5. Примеры/кейсы: минимум 1–2 развёрнутых примера с пошаговым разбором.
6. Выводы (\\section*{Выводы}): 4–7 пунктов, что важно запомнить.
7. Список литературы (\\section*{Литература}): 3–6 реальных учебников/статей по теме (не выдумывай несуществующие издания; если точных данных нет — укажи общеизвестные классические источники).
Требования: академический тон, определения терминов, формулы с пояснениями, примеры из практики."""

DOCTYPE_SUMMARY = """ТИП ДОКУМЕНТА: КОНСПЕКТ.
Структура (сжатая, без воды):
1. Заголовок темы + краткое вступление (2–4 предложения: о чём конспект).
2. Ключевые тезисы (\\section{Ключевые тезисы}): маркированный список главных идей.
3. Определения (\\section{Определения}): термины жирным + короткое точное определение.
4. Формулы (\\section{Формулы}): все ключевые формулы с расшифровкой символов.
5. Основные разделы по теме (2–4 коротких раздела, каждый — сжато, с таблицами/схемами-списками где полезно).
6. Шпаргалка/итог (\\section*{Итог}): таблица или список «самое важное за 1 страницу».
Требования: максимальная плотность информации, минимум воды, каждая страница — польза. Не пиши длинных вступлений."""

DOCTYPE_HOMEWORK = """ТИП ДОКУМЕНТА: ДОМАШНЕЕ ЗАДАНИЕ.
Структура:
1. Заголовок + инструкция (срок, формат сдачи, что оценивается — если не задано, укажи разумные: «сдать через неделю», «оценивается полнота и корректность»).
2. Задания, сгруппированные по уровню сложности:
   - Уровень 1 (базовый): 2–3 задания на воспроизведение.
   - Уровень 2 (стандартный): 2–3 задания на применение.
   - Уровень 3 (повышенный): 1–2 задания на анализ/проект.
   Каждое задание: чёткая формулировка, что нужно сделать, какие данные/условия, ожидаемый результат. Нумерация заданий обязательна.
3. Требования к оформлению работы (1 абзац).
4. Критерии оценивания (таблица: критерий — баллы).
ВАЖНО: НЕ давай решений и не раскрывай ответы. Только задания. Формулировки должны быть однозначными."""

DOCTYPE_TEST = """ТИП ДОКУМЕНТА: ТЕСТ.
Структура:
1. Заголовок + инструкции (сколько вопросов, типы, как отвечать, время если уместно).
2. Вопросы (\\section*{Вопросы} или нумерованные \\subsection/\\paragraph). Типы по запросу пользователя; если не указано — микс: ~60% с одним правильным ответом, ~25% с множественным выбором, ~15% открытые.
   - Вопрос с одним ответом: 4 варианта (а, б, в, г), только один верный.
   - Вопрос с множественным выбором: 4–5 вариантов, верных может быть несколько (укажи это в формулировке).
   - Открытый вопрос: без вариантов, ожидается краткий письменный ответ.
   Каждый вопрос — на отдельной строке/абзаце, варианты — списком enumerate/itemize с буквами.
3. Ключ ответов (\\section*{Ключ ответов}): таблица «№ вопроса — правильный ответ» + для открытых вопросов — эталонные краткие ответы (2–4 предложения).
Требования: вопросы должны проверять именно ту тему, что в запросе; дистракторы — правдоподобные, но неверные; избегай двусмысленных формулировок."""

DOCTYPE_BLOCKS: dict[DocType, str] = {
    DocType.LECTURE: DOCTYPE_LECTURE,
    DocType.SUMMARY: DOCTYPE_SUMMARY,
    DocType.HOMEWORK: DOCTYPE_HOMEWORK,
    DocType.TEST: DOCTYPE_TEST,
}

# ---------------------------------------------------------------------------
# Промт авто-ремонта
# ---------------------------------------------------------------------------

REPAIR_PROMPT_TEMPLATE = """Ты — эксперт по LaTeX. Ниже — LaTeX-документ, который НЕ скомпилировался, и лог ошибки компилятора.

Твоя задача: исправь код так, чтобы он скомпилировался БЕЗ ошибок. Верни ТОЛЬКО исправленный полный LaTeX-документ (без пояснений, допустимо в ```latex ... ```).

Правила исправления:
- Исправляй только то, что ломает компиляцию. Не меняй содержание, структуру и стиль без необходимости.
- Частые причины: незакрытые окружения (\\begin/\\end), незакрытые скобки/фигурные скобки, неэкранированные спецсимволы (& % $ # _), неизвестные пакеты/команды, ошибки в listings, неверный синтаксис таблиц.
- ЗАПРЕЩЕНО добавлять пакеты вне разрешённого списка из исходного документа. Если пакет неизвестен — удали его и замени функциональность стандартными средствами.
- Сохрани \\documentclass, преамбулу (за вычетом удалённых пакетов) и всё содержание.

--- ЛОГ ОШИБКИ КОМПИЛЯТОРА ---
{error_log}
--- КОНЕЦ ЛОГА ---

--- ИСХОДНЫЙ LATEX-КОД ---
{latex_code}
--- КОНЕЦ КОДА ---
"""

# ---------------------------------------------------------------------------
# Сборка промта
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromptContext:
    """Контекст для сборки промта генерации."""

    request_text: str
    structure_text: str = ""
    source_text: str = ""
    source_name: str = ""
    doc_type: DocType = DocType.LECTURE
    style: Style = Style.STRICT
    volume: Volume = field(default_factory=Volume)
    use_web_search: bool = False
    web_sources: tuple[tuple[str, str], ...] = ()  # (title, url)
    language: str = "ru"  # "ru" | "en"


def detect_language(text: str) -> str:
    """Определить язык текста: 'ru' если есть кириллица, иначе 'en'.

    Args:
        text: Текст запроса пользователя.

    Returns:
        'ru' или 'en'.
    """
    if not text:
        return "ru"
    cyrillic = sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")
    latin = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
    return "ru" if cyrillic > latin * 0.3 and cyrillic > 10 else ("ru" if cyrillic > 0 and latin == 0 else "en" if latin > cyrillic else "ru")


def volume_hint(ctx: PromptContext) -> str:
    """Подсказка модели про объём документа."""
    if ctx.doc_type == DocType.TEST:
        return (
            f"ОБЪЁМ: ровно {ctx.volume.questions} вопросов, в каждом вопросе с выбором — {ctx.volume.variants} варианта. "
            f"Не больше и не меньше. Ключ ответов — на все {ctx.volume.questions} вопросов."
        )
    # Оценка слов на страницу для LaTeX 11pt: ~450–550 слов
    words = ctx.volume.pages * 500
    return (
        f"ОБЪЁМ: примерно {ctx.volume.pages} страниц (около {words} слов основного текста, без титульного листа и оглавления). "
        f"Старайся попасть в диапазон ±20%. Не пиши в 2 раза короче или длиннее."
    )


def build_generation_prompt(ctx: PromptContext) -> str:
    """Собрать полный промт генерации LaTeX-документа.

    Args:
        ctx: Контекст (запрос, структура, исходник, тип, стиль, объём, поиск).

    Returns:
        Готовый промт для LLM.
    """
    lang_name = "русский" if ctx.language == "ru" else "английский (English)"
    parts: list[str] = [
        SYSTEM_PROMPT,
        "",
        STYLE_BLOCKS[ctx.style],
        "",
        DOCTYPE_BLOCKS[ctx.doc_type],
        "",
        volume_hint(ctx),
        "",
        f"ЯЗЫК ДОКУМЕНТА: {lang_name}. Весь документ (заголовки, текст, подписи) — на этом языке.",
    ]

    if ctx.structure_text.strip():
        parts += [
            "",
            "СТРУКТУРА ДОКУМЕНТА (задана пользователем — следуй ей точно, сохрани порядок и названия разделов):",
            ctx.structure_text.strip(),
        ]

    if ctx.source_text.strip():
        src_label = f" (файл: {ctx.source_name})" if ctx.source_name else ""
        parts += [
            "",
            f"ИСХОДНЫЙ МАТЕРИАЛ{src_label} (основа содержания; переработай его по запросу ниже, сохрани фактическую точность):",
            ctx.source_text.strip(),
        ]

    parts += [
        "",
        "ЗАПРОС ПОЛЬЗОВАТЕЛЯ (тема, аспекты, пожелания к содержанию и стилю изложения):",
        ctx.request_text.strip() or "(запрос не указан — сделай стандартный материал по исходному файлу)",
    ]

    if ctx.use_web_search and ctx.web_sources:
        parts += [
            "",
            "АКТУАЛЬНЫЕ ДАННЫЕ ИЗ ИНТЕРНЕТА (используй их, вплети в текст естественно; в конце документа добавь раздел «Источники» / «Sources» со списком ниже, каждый пункт — название + \\url{ссылка} + дата актуальности):",
        ]
        for title, url in ctx.web_sources:
            parts.append(f"- {title}: {url}")
        parts.append(
            "В разделе источников укажи дату: сегодняшнюю (если не знаешь — напиши «актуально на дату генерации»). "
            "Не выдумывай ссылки: используй ТОЛЬКО URL из списка выше."
        )
    elif ctx.use_web_search:
        parts.append(
            "\nВЕБ-ПОИСК был включён, но данные не найдены. Не выдумывай ссылки. "
            "В конце добавь раздел «Источники» с пометкой, что актуальные данные не удалось получить, и укажи 2–3 общеизвестных источника без URL."
        )

    parts += [
        "",
        "Начни ответ сразу с \\documentclass. Никакого текста перед ним.",
    ]
    return "\n".join(parts)


def build_repair_prompt(latex_code: str, error_log: str) -> str:
    """Собрать промт авто-ремонта LaTeX.

    Args:
        latex_code: Нескомпилировавшийся LaTeX-код.
        error_log: Лог компилятора (обрезается до 8000 символов).

    Returns:
        Промт для LLM.
    """
    # error_log может прийти как None (движок без вывода) — не падаем на срезе
    safe_log = "" if error_log is None else str(error_log)
    safe_code = "" if latex_code is None else str(latex_code)
    return REPAIR_PROMPT_TEMPLATE.format(
        error_log=safe_log[:8000] or "(лог компилятора пуст)",
        latex_code=safe_code[:60000],
    )
