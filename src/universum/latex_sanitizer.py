"""Санитайзер LaTeX, сгенерированного нейросетью.

Нейросеть регулярно ломает сборку типичными ошибками, которые видны ещё до
компиляции. Здесь они исправляются **детерминированно**, до Tectonic:

* markdown-разметка (``**жирный**``, `` `код` ``, заголовки ``#``, ссылки
  ``[текст](url)``, горизонтальные линии, ``` ```latex ```-обёртки);
* неэкранированные спецсимволы в обычном тексте (``%``, ``&``, ``#``, ``_``,
  ``^``) — при этом математика, таблицы, листинги, ``\\label``/``\\ref``/``\\url``
  и уже экранированные символы не трогаются;
* незакрытые фигурные скобки, ``$`` и окружения ``\\begin``/``\\end``;
* запрещённые конструкции: ``\\documentclass``/``\\usepackage``/``\\maketitle``/
  ``\\includegraphics``/``\\input``/``tikzpicture``/``minted`` и т.п.;
* вложенные блоки-плашки (``definition`` внутри ``definition``) — tcolorbox их
  не переваривает;
* «мусорные» unicode-символы (пробелы нулевой ширины, мягкие дефисы,
  типографские кавычки).

Все функции чистые и идемпотентные: повторная обработка уже очищенного текста
ничего не меняет (это важно — санитайзер вызывается и после авто-ремонта).
"""

from __future__ import annotations

import re

__all__ = [
    "sanitize_llm_latex",
    "emergency_latex",
    "undefined_commands_from_log",
    "strip_commands",
    "KNOWN_ENVIRONMENTS",
]


# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------

# Окружения, которые точно есть в нашей преамбуле/ядре LaTeX.
KNOWN_ENVIRONMENTS: frozenset[str] = frozenset(
    {
        # наши блоки-акценты (tcolorbox / fallback-абзацы)
        "definition", "theorem", "example", "important", "keyidea",
        # списки и выравнивание
        "itemize", "enumerate", "description", "center", "flushleft", "flushright",
        "quote", "quotation", "verse", "minipage",
        # структура страницы и таблицы
        "table", "table*", "figure", "figure*", "tabular", "tabular*", "tabularx",
        "tabulary", "longtable", "thebibliography",
        # математика
        "equation", "equation*", "align", "align*", "alignat", "alignat*",
        "gather", "gather*", "multline", "multline*", "split", "aligned", "gathered",
        "cases", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix",
        "smallmatrix", "displaymath", "math", "array",
        # код и доказательства
        "lstlisting", "verbatim", "verbatim*", "proof",
    }
)

_MATH_ENVS: frozenset[str] = frozenset(
    {
        "equation", "equation*", "align", "align*", "alignat", "alignat*",
        "gather", "gather*", "multline", "multline*", "split", "aligned", "gathered",
        "cases", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix",
        "smallmatrix", "displaymath", "math", "array",
    }
)

_TABULAR_ENVS: frozenset[str] = frozenset(
    {"tabular", "tabular*", "tabularx", "tabulary", "longtable", "array"}
)

_VERBATIM_ENVS: frozenset[str] = frozenset({"lstlisting", "verbatim", "verbatim*"})

# Блоки-плашки: tcolorbox не поддерживает вложенность «плашка в плашке»
_BLOCK_ENVS: frozenset[str] = frozenset(
    {"definition", "theorem", "example", "important", "keyidea"}
)

# Команды и число их аргументов, которые копируются «как есть»:
# URL, метки, размеры. Экранирование сломало бы \▸url/\▸label (а для \▸href —
# и ссылку целиком).
_RAW_ARGS: dict[str, int] = {
    "url": 1, "href": 2, "hyperref": 1, "hyperlink": 2,
    "label": 1, "ref": 1, "eqref": 1, "pageref": 1, "autoref": 1, "nameref": 1,
    "cite": 1, "citep": 1, "citet": 1, "citeauthor": 1, "citeyear": 1, "nocite": 1,
    "includegraphics": 1, "input": 1, "include": 1, "includeonly": 1,
    "bibliography": 1, "bibliographystyle": 1, "lstinputlisting": 1,
    "hspace": 1, "vspace": 1, "rule": 2, "raisebox": 1,
    "setlength": 2, "addtolength": 2, "setcounter": 2, "addtocounter": 2,
    "textcolor": 1, "color": 1, "definecolor": 3, "setlist": 2,
    "multicolumn": 3, "fontsize": 2,
}

# Содержимое этих окружений выбрасываем целиком (это не текст, а рисунки)
_DROP_CONTENT_ENVS: frozenset[str] = frozenset({"tikzpicture", "picture", "pspicture"})

# Определения команд/окружений: копируем «как есть» (#1 внутри тела ломать нельзя)
_DEFINITION_COMMANDS: frozenset[str] = frozenset(
    {
        "newcommand", "renewcommand", "providecommand", "def", "gdef", "edef", "xdef",
        "DeclareMathOperator", "newenvironment", "renewenvironment",
    }
)


# ---------------------------------------------------------------------------
# 1. Нормализация unicode и markdown
# ---------------------------------------------------------------------------

_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
_QUOTE_MAP = {
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2018": "'", "\u2019": "'", "\u2032": "'",
    "\u2212": "-",
    "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u2007": " ",
}


def _normalize_unicode(text: str) -> str:
    """Убрать невидимые символы и заменить «типографские» кавычки."""
    out = _ZERO_WIDTH_RE.sub("", text)
    for src, dst in _QUOTE_MAP.items():
        out = out.replace(src, dst)
    return out


_FENCE_INLINE_RE = re.compile(r"```(?:latex|tex)?[ \t]*", re.IGNORECASE)
_MD_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_MD_UNDERSCORE_BOLD_RE = re.compile(r"(?<![\w\\])__(?=\S)(.+?)(?<=\S)__(?![\w])", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<![\w*\\])\*(?=\S)([^*\n]{1,200}?)(?<=\S)\*(?![\w*])")
_MD_CODE_RE = re.compile(r"(?<!`)`([^`\n]{1,300})`(?!`)")
_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^)\s]{1,300})\)")
_MD_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,3})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_MD_RULE_RE = re.compile(r"^[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
_MD_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.MULTILINE)


_TEXT_ESCAPES = {"%": r"\%", "&": r"\&", "#": r"\#", "_": r"\_", "^": r"\textasciicircum{}"}


def _escape_text_run(text: str) -> str:
    """Экранировать спецсимволы в коротком фрагменте обычного текста."""
    return "".join(_TEXT_ESCAPES.get(ch, ch) for ch in text)


def _md_code_to_texttt(match: re.Match[str]) -> str:
    """`` `код` `` → ``\\texttt{код}`` (спецсимволы внутри экранируются)."""
    return "\\texttt{" + _escape_text_run(match.group(1)) + "}"


def _md_link_to_href(match: re.Match[str]) -> str:
    """Markdown-ссылка → ``\\href{url}{текст}``."""
    title, url = match.group(1), match.group(2)
    return "\\href{" + url.replace("%", "\\%") + "}{" + _escape_text_run(title) + "}"


def _convert_markdown(text: str) -> str:
    """Перевести типичную markdown-разметку в LaTeX."""
    out = _FENCE_INLINE_RE.sub("", text)
    out = _MD_LINK_RE.sub(_md_link_to_href, out)
    out = _MD_CODE_RE.sub(_md_code_to_texttt, out)
    out = _MD_UNDERSCORE_BOLD_RE.sub(
        lambda m: "\\textbf{" + _escape_text_run(m.group(1)) + "}", out
    )
    out = _MD_BOLD_RE.sub(lambda m: "\\textbf{" + _escape_text_run(m.group(1)) + "}", out)
    out = _MD_ITALIC_RE.sub(lambda m: "\\textit{" + _escape_text_run(m.group(1)) + "}", out)

    def _heading(match: re.Match[str]) -> str:
        level = len(match.group(1))
        title = _escape_text_run(match.group(2).strip())
        command = {1: "section", 2: "subsection", 3: "subsubsection"}[level]
        return f"\\{command}{{{title}}}"

    out = _MD_HEADING_RE.sub(_heading, out)
    out = _MD_RULE_RE.sub("", out)
    out = _MD_QUOTE_RE.sub("", out)
    return out


# ---------------------------------------------------------------------------
# 2. Запрещённые конструкции
# ---------------------------------------------------------------------------

_BANNED_LINE_RE = re.compile(
    r"^[ \t]*\\(?:documentclass|usepackage|RequirePackage|maketitle"
    r"|bibliography|bibliographystyle|printbibliography|input|include|includeonly"
    r"|lstinputlisting)\b[^\n]*$",
    re.MULTILINE,
)
_TITLE_COMMANDS_RE = re.compile(
    r"\\(?:title|author|date)\s*(?:\[[^\]]*\])?\s*\{[^{}]*\}", re.DOTALL
)
_TITLEPAGE_RE = re.compile(r"\\begin\{titlepage\}.*?\\end\{titlepage\}", re.DOTALL)
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{[^{}]*\}")
_TIKZ_RE = re.compile(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", re.DOTALL)
_MINTED_ENV_RE = re.compile(r"\\begin\{minted\}\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
_MINTED_END_RE = re.compile(r"\\end\{minted\}")
_MINTINLINE_RE = re.compile(r"\\mintinline\s*(?:\[[^\]]*\])?\s*\{[^{}]*\}")


def _remove_forbidden(text: str) -> str:
    """Вырезать конструкции, которые ломают компиляцию в нашей преамбуле."""
    out = _TITLEPAGE_RE.sub("", text)
    out = _TIKZ_RE.sub("", out)
    out = _BANNED_LINE_RE.sub("", out)
    out = _TITLE_COMMANDS_RE.sub("", out)
    out = _INCLUDEGRAPHICS_RE.sub("", out)
    out = _MINTINLINE_RE.sub("", out)
    # minted → lstlisting (пакет minted не подключён и требует shell-escape)
    out = _MINTED_ENV_RE.sub(
        lambda m: "\\begin{lstlisting}[language=" + (m.group(1) or "text") + "]", out
    )
    out = _MINTED_END_RE.sub(lambda _m: "\\end{lstlisting}", out)
    out = out.replace("\\begin{document}", "").replace("\\end{document}", "")
    return out


# ---------------------------------------------------------------------------
# 3. Основной сканер: экранирование + балансировка
# ---------------------------------------------------------------------------


class _Scanner:
    """Посимвольный разбор тела LaTeX с исправлением типичных ошибок."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.depth = 0                            # глубина фигурных скобок
        self.envs: list[tuple[str, bool]] = []    # (имя, выводить ли \end)
        self.math = 0                             # >0 — внутри $…$ / \[…\] / \(…\)
        self.display_math = False                 # внутри $$…$$
        self.verbatim: str | None = None
        self.line_depth = 0                       # глубина скобок на начале строки
        self.dropped = 0                          # сколько «битых» конструкций выброшено

    # -- утилиты ---------------------------------------------------------
    def _emit(self, text: str) -> None:
        self.out.append(text)

    def _in_tabular(self) -> bool:
        return any(name in _TABULAR_ENVS for name, _ in self.envs)

    def _in_block(self) -> bool:
        return any(name in _BLOCK_ENVS and emit for name, emit in self.envs)

    def _in_math_env(self) -> bool:
        return any(name in _MATH_ENVS for name, _ in self.envs)

    @staticmethod
    def _at_line_start(text: str, pos: int) -> bool:
        """Стоит ли позиция в начале строки (перед ней только пробелы)."""
        line_start = text.rfind("\n", 0, pos) + 1
        return not text[line_start:pos].strip()

    @staticmethod
    def _read_group(text: str, pos: int) -> tuple[int, str | None]:
        """Прочитать ``{...}`` начиная с позиции ``pos`` (пробелы разрешены).

        Returns:
            (позиция после группы, содержимое группы) или (pos, None).
        """
        i = pos
        n = len(text)
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n or text[i] != "{":
            return pos, None
        depth = 0
        start = i
        while i < n:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1, text[start + 1: i]
            i += 1
        return n, text[start + 1: n]

    # -- основной цикл ---------------------------------------------------
    def run(self, text: str) -> None:
        i = 0
        n = len(text)
        while i < n:
            if self.verbatim is not None:
                i = self._consume_verbatim(text, i)
                continue
            ch = text[i]
            if ch == "\\":
                i = self._consume_command(text, i)
                continue
            if ch == "$":
                i = self._consume_dollar(text, i)
                continue
            if ch == "\n":
                self._emit_newline()
                i += 1
                continue
            if ch == "{" and not self.math and not self.display_math:
                self.depth += 1
                self._emit("{")
                i += 1
                continue
            if ch == "}":
                if self.depth > 0:
                    self.depth -= 1
                    self._emit("}")
                else:
                    self.dropped += 1
                i += 1
                continue
            i = self._consume_plain(text, i)

    def _emit_newline(self) -> None:
        """Закрыть незакрытые на этой строке группы и перейти на новую строку."""
        if self.depth > self.line_depth:
            self._emit("}" * (self.depth - self.line_depth))
            self.depth = self.line_depth
        self._emit("\n")

    def _consume_dollar(self, text: str, i: int) -> int:
        """``$`` / ``$$`` — переключение математического режима."""
        if text.startswith("$$", i):
            self._emit("$$")
            self.display_math = not self.display_math
            return i + 2
        self._emit("$")
        if self.math > 0:
            self.math -= 1
        elif not self.display_math:
            self.math += 1
        return i + 1

    def _consume_plain(self, text: str, i: int) -> int:
        """Обычный текст; опасные символы экранируем."""
        n = len(text)
        ch = text[i]
        if ch == "%":
            if self._at_line_start(text, i):
                end = text.find("\n", i)
                end = n if end == -1 else end
                self._emit(text[i:end])  # настоящий комментарий
                return end
            self._emit(r"\%")
            return i + 1
        if self.math or self.display_math or self._in_math_env():
            self._emit(ch)  # внутри формул ничего не экранируем
            return i + 1
        if ch in _TEXT_ESCAPES:
            if ch == "&" and self._in_tabular():
                self._emit("&")  # выравнивание в таблице
                return i + 1
            self._emit(_TEXT_ESCAPES[ch])
            return i + 1
        self._emit(ch)
        return i + 1

    # -- команды ---------------------------------------------------------
    def _consume_command(self, text: str, i: int) -> int:
        n = len(text)
        if i + 1 >= n:
            self._emit("\\")
            return i + 1
        nxt = text[i + 1]
        if nxt == "\\":  # разрыв строки
            self._emit("\\\\")
            return i + 2
        if nxt in "%&#_${}":
            self._emit(text[i: i + 2])  # уже экранировано
            return i + 2
        if nxt in "[(":
            self.math += 1
            self._emit(text[i: i + 2])
            return i + 2
        if nxt in "])":
            if self.math > 0:
                self.math -= 1
            self._emit(text[i: i + 2])
            return i + 2
        if not (nxt.isalpha() or nxt == "@"):
            self._emit(text[i: i + 2])  # \, \; \! \  и прочее
            return i + 2
        j = i + 1
        while j < n and (text[j].isalpha() or text[j] == "@"):
            j += 1
        return self._handle_named(text, i, j, text[i + 1: j])

    def _handle_named(self, text: str, start: int, after: int, name: str) -> int:
        """Обработать именованную команду ``\\name``."""
        if name == "verb":
            return self._consume_verb_inline(text, start, after)
        if name == "begin":
            return self._begin_env(text, after)
        if name == "end":
            return self._end_env(text, after)
        if name in _DEFINITION_COMMANDS:
            return self._consume_definition(text, start, after)
        self._emit("\\" + name)
        raw_groups = _RAW_ARGS.get(name)
        if raw_groups:
            return self._consume_raw_args(text, after, raw_groups)
        return after

    def _consume_raw_args(self, text: str, after: int, count: int) -> int:
        """Скопировать следующие ``count`` групп без экранирования."""
        pos = after
        for _ in range(count):
            newpos, group = self._read_group(text, pos)
            if group is None:
                break
            self._emit(text[pos:newpos])
            pos = newpos
        return pos

    def _consume_verb_inline(self, text: str, start: int, after: int) -> int:
        """``\\verb|...|`` — содержимое не трогаем."""
        n = len(text)
        if after >= n:
            self._emit(text[start:after])
            return after
        delim = text[after]
        end = text.find(delim, after + 1)
        if end == -1:
            self._emit(text[start:after])
            return after
        self._emit(text[start: end + 1])
        return end + 1

    def _consume_definition(self, text: str, start: int, after: int) -> int:
        """``\\newcommand{\\x}[1]{...}`` — копируем целиком, не экранируя ``#1``."""
        n = len(text)
        pos = after
        newpos, group = self._read_group(text, pos)
        if group is None:
            self._emit(text[start:after])
            return after
        while True:
            probe = newpos
            while probe < n and text[probe] in " \t\r\n":
                probe += 1
            if probe < n and text[probe] == "[":
                close = text.find("]", probe)
                if close == -1:
                    break
                newpos = close + 1
                continue
            break
        body_end, _body = self._read_group(text, newpos)
        self._emit(text[start:body_end])
        return body_end

    def _consume_verbatim(self, text: str, i: int) -> int:
        env = self.verbatim or ""
        marker = "\\end{%s}" % env
        idx = text.find(marker, i)
        if idx == -1:
            self._emit(text[i:])
            return len(text)
        self._emit(text[i: idx + len(marker)])
        self.verbatim = None
        self._pop_env(env)
        return idx + len(marker)

    def _pop_env(self, env: str) -> None:
        for pos in range(len(self.envs) - 1, -1, -1):
            if self.envs[pos][0] == env:
                del self.envs[pos]
                return

    # -- окружения -------------------------------------------------------
    def _begin_env(self, text: str, after: int) -> int:
        newpos, env = self._read_group(text, after)
        if env is None:
            self._emit("\\begin")
            return after
        env = env.strip()
        if env in _DROP_CONTENT_ENVS:
            end_marker = "\\end{%s}" % env
            end = text.find(end_marker, newpos)
            self.dropped += 1
            return newpos if end == -1 else end + len(end_marker)
        if env in _BLOCK_ENVS and self._in_block():
            # плашка внутри плашки: tcolorbox падает — внутреннюю превращаем
            # в обычный текст (begin и парный end выкидываем)
            self.envs.append((env, False))
            self.dropped += 1
            return newpos
        if env not in KNOWN_ENVIRONMENTS:
            self.envs.append((env, False))
            self.dropped += 1
            return newpos
        if env in _VERBATIM_ENVS:
            self._emit("\\begin{%s}" % env)
            probe = newpos
            while probe < len(text) and text[probe] in " \t":
                probe += 1
            if env == "lstlisting" and probe < len(text) and text[probe] == "[":
                close = text.find("]", probe)
                if close != -1:
                    self._emit(text[probe: close + 1])
                    newpos = close + 1
            self.envs.append((env, True))
            self.verbatim = env
            return newpos
        self._emit("\\begin{%s}" % env)
        self.envs.append((env, True))
        return newpos

    def _end_env(self, text: str, after: int) -> int:
        newpos, env = self._read_group(text, after)
        if env is None:
            self._emit("\\end")
            return after
        env = env.strip()
        if env in _DROP_CONTENT_ENVS:
            return newpos
        names = [name for name, _ in self.envs]
        if env not in names:
            self.dropped += 1  # \end без \begin — выбрасываем
            return newpos
        while self.envs and self.envs[-1][0] != env:
            name, emit = self.envs.pop()
            if emit:
                self._emit("\\end{%s}" % name)
        name, emit = self.envs.pop()
        if emit:
            self._emit("\\end{%s}" % env)
        else:
            self.dropped += 1
        return newpos

    # -- финализация -----------------------------------------------------
    def finish(self) -> str:
        for name, emit in reversed(self.envs):
            if emit:
                self._emit("\\end{%s}" % name)
        self.envs.clear()
        self.verbatim = None
        if self.depth > 0:
            self._emit("}" * self.depth)
            self.depth = 0
        return "".join(self.out)


# ---------------------------------------------------------------------------
# 4. Публичное API
# ---------------------------------------------------------------------------

_UNESCAPED_DOLLAR_RE = re.compile(r"(?<!\\)\$")


def _balance_dollars(text: str) -> str:
    """Сделать число незаэкранированных ``$`` чётным («Missing $ inserted»)."""
    positions = [m.start() for m in _UNESCAPED_DOLLAR_RE.finditer(text)]
    if len(positions) % 2 == 0:
        return text
    last = positions[-1]
    return text[:last] + "\\$" + text[last + 1:]


def sanitize_llm_latex(body: str) -> str:
    """Очистить тело LaTeX от типичных ошибок нейросети.

    Функция идемпотентна: повторный вызов на очищенном тексте ничего не меняет.

    Args:
        body: Тело документа от LLM (может быть ``None``/пустым).

    Returns:
        Очищенное тело (всегда строка).
    """
    text = "" if body is None else str(body)
    if not text.strip():
        return ""
    text = _normalize_unicode(text)
    text = _convert_markdown(text)
    text = _remove_forbidden(text)
    scanner = _Scanner()
    scanner.run(text)
    result = scanner.finish()
    result = _balance_dollars(result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ---------------------------------------------------------------------------
# 5. Аварийный режим: оставить только заведомо известные команды
# ---------------------------------------------------------------------------

_ALLOWED_COMMANDS: frozenset[str] = frozenset(
    """
    makelecturetitle universumdoctype term hl qed item caption
    section subsection subsubsection paragraph
    textbf textit emph textrm textsf texttt textsc textsl textup textnormal
    textcolor colorbox fbox mbox hbox makebox parbox raisebox
    footnote label ref eqref pageref autoref nameref url href hyperref hyperlink
    newline linebreak par noindent centering raggedright raggedleft
    smallskip medskip bigskip hfill vfill quad qquad hspace vspace hline cline
    clearpage newpage pagebreak nopagebreak tableofcontents
    textbackslash textasciitilde textasciicircum textperiodcentered textdegree
    ldots dots cdots vdots ddots LaTeX TeX today
    alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa
    lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi
    psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
    sum prod coprod int iint iiint oint bigcup bigcap bigoplus bigotimes
    infty partial nabla forall exists nexists neg emptyset varnothing
    cdot cdots times div pm mp ast star circ bullet
    leq le geq ge neq ne approx equiv sim simeq cong propto
    to rightarrow leftarrow Rightarrow Leftarrow leftrightarrow Leftrightarrow
    mapsto implies iff uparrow downarrow Uparrow Downarrow
    in notin ni subset subseteq supset supseteq cup cap setminus
    land lor wedge vee oplus otimes perp parallel mid vert Vert
    langle rangle lfloor rfloor lceil rceil lbrace rbrace lbrack rbrack
    frac dfrac tfrac sqrt left right big Big bigg Bigg
    mathbb mathcal mathrm mathbf mathit mathsf mathtt operatorname
    lim log ln lg sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh
    exp max min sup inf det gcd bmod mod pmod
    hat bar vec dot ddot tilde overline underline overbrace underbrace
    binom choose displaystyle textstyle scriptstyle
    hbar imath jmath ell wp Re Im aleph prime ldotp cdotp colon dagger ddag
    sharp flat natural top bot triangle square surd lnot notag nonumber tag
    phantom hphantom vphantom mathstrut smash llap rlap ensuremath
    overrightarrow overleftarrow underrightarrow xrightarrow xleftarrow
    toprule midrule bottomrule cmidrule addlinespace tabularnewline
    tiny scriptsize footnotesize small normalsize large Large LARGE huge Huge
    bfseries itshape sffamily ttfamily rmfamily mdseries upshape scshape
    textsuperscript textsubscript smallskip medskip bigskip
    begin end
    """.split()
)

_COMMAND_RE = re.compile(r"\\([A-Za-z]+)")
_DEFINITION_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand|DeclareMathOperator"
    r"|newenvironment|renewenvironment|def|gdef|edef|xdef)\b"
)


def _strip_definitions(text: str) -> str:
    """Удалить определения команд/окружений целиком (``\\newcommand…{…}``)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        match = _DEFINITION_RE.search(text, i)
        if not match:
            out.append(text[i:])
            break
        out.append(text[i: match.start()])
        pos = match.end()
        groups = 0
        while pos < n:
            while pos < n and text[pos] in " \t\r\n":
                pos += 1
            if pos < n and text[pos] == "[":
                close = text.find("]", pos)
                pos = n if close == -1 else close + 1
                continue
            if pos < n and text[pos] == "{":
                depth = 0
                while pos < n:
                    if text[pos] == "\\":
                        pos += 2
                        continue
                    if text[pos] == "{":
                        depth += 1
                    elif text[pos] == "}":
                        depth -= 1
                        if depth == 0:
                            pos += 1
                            break
                    pos += 1
                groups += 1
                if groups >= 2:  # имя + тело
                    break
                continue
            break
        i = pos
    return "".join(out)


def emergency_latex(body: str) -> str:
    """«Аварийная» очистка: оставить только заведомо рабочие команды.

    Используется как последняя попытка сборки, когда ни очистка, ни авто-ремонт
    не помогли. Неизвестные команды удаляются (их аргументы остаются текстом,
    скобки сбалансированы), неизвестные окружения превращаются в обычные
    абзацы. Оформление может пострадать, но PDF соберётся.

    Args:
        body: Тело документа (уже очищенное или нет).

    Returns:
        Тело, в котором нет незнакомых макросов и окружений.
    """
    text = sanitize_llm_latex(body)
    if not text:
        return ""
    text = _strip_definitions(text)
    text = _COMMAND_RE.sub(
        lambda m: m.group(0) if m.group(1) in _ALLOWED_COMMANDS else "", text
    )

    def _env(match: re.Match[str]) -> str:
        env = match.group(2).strip()
        return match.group(0) if env in KNOWN_ENVIRONMENTS else ""

    text = re.sub(r"\\(begin|end)\{([^{}]+)\}", _env, text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 6. Точечное лечение по логу компилятора
# ---------------------------------------------------------------------------

_LOG_COMMAND_RE = re.compile(r"l\.\d+\s+\\?([A-Za-z]+)")


def undefined_commands_from_log(log: str, limit: int = 8) -> list[str]:
    """Вытащить из лога LaTeX команды, на которые ругается движок.

    Ищет «Undefined control sequence» и следующую строку вида ``l.42 \\foo``.

    Args:
        log: Лог компилятора.
        limit: Сколько имён вернуть максимум.

    Returns:
        Список имён команд без обратной косой черты.
    """
    text = "" if log is None else str(log)
    names: list[str] = []
    if "Undefined control sequence" not in text and "Undefined control" not in text:
        return names
    for match in _LOG_COMMAND_RE.finditer(text):
        name = match.group(1)
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def strip_commands(text: str, names: list[str] | tuple[str, ...] | set[str]) -> str:
    """Удалить команды по именам, оставив их аргументы текстом.

    Args:
        text: LaTeX-код.
        names: Имена команд (без обратной косой черты).

    Returns:
        Код без указанных команд.
    """
    if not text or not names:
        return "" if text is None else str(text)
    out = str(text)
    for name in names:
        if not name:
            continue
        out = re.sub(r"\\" + re.escape(name) + r"(?![A-Za-z])", "", out)
    return out
