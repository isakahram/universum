"""Промты и LaTeX-шаблон Universum.

Архитектура генерации:
1. Нейросеть получает системный промт и возвращает **только тело документа**
   (всё, что должно стоять между ``\\begin{document}`` и ``\\end{document}``).
2. Все пакеты, цвета, шрифты, окружения-блоки и титульный лист задаются
   **жёстко в этом модуле** (функция :func:`build_preamble`) — модель не может
   сломать преамбулу.
3. :func:`wrap_document` собирает готовый компилируемый документ:
   магическая строка ``% !TEX program = pdflatex`` + преамбула стиля + тело.

Стили используют одинаковый набор команд (``definition``, ``theorem``,
``example``, ``important``, ``keyidea``, ``\\makelecturetitle``), поэтому
смена стиля не требует перегенерации текста — меняется только преамбула.

Модуль содержит:
- магическую строку и преамбулы трёх стилей (:func:`build_preamble`);
- сборку документа (:func:`wrap_document`);
- системный промт (роль + жёсткие правила компилируемости);
- блоки стилей (что и как акцентировать);
- блоки типов документов (лекция / конспект / ДЗ / тест);
- промт авто-ремонта LaTeX;
- сборку полного промта :func:`build_generation_prompt`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import DocType, Style, Volume

# Магическая строка: подсказывает редакторам (TeXstudio/TeXShop) движок.
# Tectonic её игнорирует (он XeTeX), поэтому преамбула ниже совместима и с
# pdflatex (T2A + inputenc), и с xelatex/lualatex (fontspec) через iftex.
MAGIC_COMMENT = "% !TEX program = pdflatex"


# ---------------------------------------------------------------------------
# Палитры стилей
# ---------------------------------------------------------------------------

_PALETTES: dict[Style, dict[str, str]] = {
    Style.COLORFUL: {
        "primary": "2E3192",    # глубокий синий
        "secondary": "F15A24",  # оранжевый акцент
        "accent": "00B4A6",     # бирюзовый
        "soft": "F4F6FB",       # нежный фон
        "highlight": "FFD166",  # жёлтый маркер
        "textgray": "3A3A3A",
    },
    Style.STRICT: {
        "primary": "000000",
        "secondary": "333333",
        "accent": "555555",
        "soft": "F7F7F7",
        "highlight": "E6E6E6",
        "textgray": "333333",
    },
    Style.PLAIN: {
        "primary": "000000",
        "secondary": "000000",
        "accent": "000000",
        "soft": "FFFFFF",
        "highlight": "F2F2F2",
        "textgray": "000000",
    },
}


# ---------------------------------------------------------------------------
# Преамбула: общая часть
# ---------------------------------------------------------------------------

_PREAMBLE_COMMON = r"""
\usepackage{iftex}
\ifPDFTeX
  % pdflatex: 8-битные шрифты. T2A + Latin Modern дают кириллицу.
  \usepackage[T2A]{fontenc}
  \usepackage[utf8]{inputenc}
  \usepackage{lmodern}
\else
  % xelatex / lualatex / Tectonic: только стандартный fontspec и DejaVu.
  % DejaVu гарантированно содержит кириллицу и есть в бандле TeX Live/Tectonic.
  % Шрифт ищем по имени ФАЙЛА (kpse), а не по имени семейства: в Tectonic
  % fontconfig может быть недоступен («Cannot load default config file»),
  % поэтому \setmainfont{DejaVu Serif} по имени там не работает.
  \usepackage{fontspec}
  \IfFontExistsTF{DejaVuSerif.ttf}
    {\setmainfont{DejaVuSerif.ttf}}
    {\IfFontExistsTF{LiberationSerif-Regular.ttf}
      {\setmainfont{LiberationSerif-Regular.ttf}}
      {\typeout{UNIVERSUM: WARNING DejaVu Serif not found, Cyrillic may be missing}}}
  \IfFontExistsTF{DejaVuSans.ttf}
    {\setsansfont{DejaVuSans.ttf}}
    {\IfFontExistsTF{LiberationSans-Regular.ttf}
      {\setsansfont{LiberationSans-Regular.ttf}}
      {\typeout{UNIVERSUM: WARNING DejaVu Sans not found}}}
  \IfFontExistsTF{DejaVuSansMono.ttf}
    {\setmonofont{DejaVuSansMono.ttf}}
    {\typeout{UNIVERSUM: WARNING DejaVu Sans Mono not found}}
\fi
\usepackage[@BABELOPTS@]{babel}
\usepackage[margin=2.2cm,top=2.5cm,bottom=2.5cm]{geometry}
\usepackage{microtype}
\usepackage{amsmath,amssymb}
\usepackage{amsthm}   % \qed и окружение proof
\usepackage{graphicx}
\usepackage{booktabs}   % \toprule/\midrule/\bottomrule
\usepackage{tabularx}   % таблицы с переносом текста
\usepackage{listings}   % листинги кода (не minted: не нужен shell-escape)
\usepackage[table,dvipsnames]{xcolor}
\usepackage{enumitem}
\usepackage{fancyhdr}
\usepackage{titlesec}
% ---------- палитра ----------
\definecolor{primary}{HTML}{@PRIMARY@}
\definecolor{secondary}{HTML}{@SECONDARY@}
\definecolor{accent}{HTML}{@ACCENT@}
\definecolor{soft}{HTML}{@SOFT@}
\definecolor{highlight}{HTML}{@HIGHLIGHT@}
\definecolor{textgray}{HTML}{@TEXTGRAY@}

% ---------- листинги кода ----------
\lstset{
  basicstyle=\ttfamily\small,
  breaklines=true,
  frame=single,
  numbers=left,
  numberstyle=\tiny,
  tabsize=4,
  showstringspaces=false,
  extendedchars=true,
  inputencoding=utf8
}

% ---------- вспомогательные команды ----------
\newcommand{\term}[1]{\textcolor{primary}{\textbf{#1}}}
\newcommand{\hl}[1]{\colorbox{highlight!60}{#1}}
"""

_COMMON_TAIL = r"""
\usepackage{hyperref}
\hypersetup{
  colorlinks=true,
  linkcolor=primary,
  urlcolor=secondary,
  citecolor=accent
}
"""


# ---------------------------------------------------------------------------
# Преамбула: блоки-окружения
# ---------------------------------------------------------------------------

# Красочный: tcolorbox с цветными плашками и иконками
_BOXES_COLORFUL = r"""
% ============================================================
%   КРАСИВЫЕ БЛОКИ (tcolorbox) + запасной вариант без tcolorbox
% ============================================================
\newif\ifuniversumfancy
\IfFileExists{tcolorbox.sty}{\universumfancytrue}{\universumfancyfalse}
\ifuniversumfancy
  \usepackage[most]{tcolorbox}
  \tcbset{
    lecbox/.style={
      enhanced, breakable,
      colback=soft, colframe=primary, coltitle=white,
      fonttitle=\bfseries\sffamily,
      boxrule=0pt, leftrule=4pt,
      arc=4pt, outer arc=4pt,
      left=10pt, right=10pt, top=8pt, bottom=8pt,
      attach boxed title to top left={xshift=8pt, yshift=-10pt},
      boxed title style={colback=primary, sharp corners, boxrule=0pt, arc=2pt,
        left=6pt, right=6pt, top=3pt, bottom=3pt},
      top=16pt
    }
  }
  \newtcolorbox{definition}[1][]{lecbox, title={\iconBulb Определение},
    colframe=accent, boxed title style={colback=accent}, #1}
  \newtcolorbox{theorem}[1][]{lecbox, title={\iconBook Теорема}, #1}
  \newtcolorbox{example}[1][]{lecbox, title={\iconFlask Пример},
    colframe=secondary, boxed title style={colback=secondary}, #1}
  \newtcolorbox{important}[1][]{lecbox, title={\iconWarn Важно},
    colframe=secondary, boxed title style={colback=secondary},
    colback=highlight!25, #1}
  \newtcolorbox{keyidea}[1][]{enhanced, colback=primary!8, colframe=primary,
    boxrule=1.5pt, arc=8pt, left=12pt, right=12pt, top=10pt, bottom=10pt,
    fontupper=\itshape, borderline west={4pt}{0pt}{secondary}, #1}
\else
  \universumplainboxes
\fi
"""

# Строгий: те же блоки, но монохромные
_BOXES_STRICT = r"""
% ============================================================
%   БЛОКИ (tcolorbox, монохром) + запасной вариант без tcolorbox
% ============================================================
\newif\ifuniversumfancy
\IfFileExists{tcolorbox.sty}{\universumfancytrue}{\universumfancyfalse}
\ifuniversumfancy
  \usepackage[most]{tcolorbox}
  \tcbset{
    lecbox/.style={
      breakable, enhanced,
      colback=white, colframe=primary!70, coltitle=black,
      fonttitle=\bfseries\sffamily,
      boxrule=0.6pt, arc=0pt, outer arc=0pt,
      left=8pt, right=8pt, top=6pt, bottom=6pt
    }
  }
  \newtcolorbox{definition}[1][]{lecbox, title={Определение}, #1}
  \newtcolorbox{theorem}[1][]{lecbox, title={Теорема}, #1}
  \newtcolorbox{example}[1][]{lecbox, title={Пример}, #1}
  \newtcolorbox{important}[1][]{lecbox, title={Важно}, colback=soft, #1}
  \newtcolorbox{keyidea}[1][]{lecbox, title={Ключевая мысль}, #1}
\else
  \universumplainboxes
\fi
"""

# Общий «минимальный» набор окружений (без tcolorbox) — используется
# и стилем «без стиля», и как fallback, если tcolorbox недоступен.
_PLAIN_BOXES = r"""
% ============================================================
%   БЛОКИ без tcolorbox (минимальное оформление)
% ============================================================
\newcommand{\universumplainboxes}{%
  \newenvironment{definition}[1][]{\par\medskip\noindent\textbf{Определение.}\ \itshape}{\par\medskip}%
  \newenvironment{theorem}[1][]{\par\medskip\noindent\textbf{Теорема.}\ \itshape}{\par\medskip}%
  \newenvironment{example}[1][]{\par\medskip\noindent\textbf{Пример.}\ \normalfont}{\par\medskip}%
  \newenvironment{important}[1][]{\par\medskip\noindent\textbf{Важно.}\ \normalfont}{\par\medskip}%
  \newenvironment{keyidea}[1][]{\par\medskip\noindent\itshape}{\par\medskip}%
}
"""


# ---------------------------------------------------------------------------
# Преамбула: иконки
# ---------------------------------------------------------------------------

_ICONS_LOAD = r"""
% ---------- иконки: грузим только если пакет есть ----------
\IfFileExists{fontawesome5.sty}{\usepackage{fontawesome5}}{}
"""

_ICONS_MACROS = r"""
% ---------- иконки ----------
% Каждая иконка проверяется через \ifcsname: если пакета нет или в этой версии
% fontawesome5 глифа нет (например \faCalendarAlt), команда просто пустая
% и документ всё равно собирается (иначе «Undefined control sequence»).
\newcommand{\universumicon}[1]{\ifcsname #1\endcsname\csname #1\endcsname\enspace\fi}
\newcommand{\iconBulb}{\universumicon{faLightbulb}}
\newcommand{\iconBook}{\universumicon{faBookOpen}}
\newcommand{\iconFlask}{\universumicon{faFlask}}
\newcommand{\iconWarn}{\universumicon{faExclamationTriangle}}
\newcommand{\iconGrad}{\universumicon{faGraduationCap}}
\newcommand{\iconUser}{\ifcsname faUserGraduate\endcsname\faUserGraduate\enspace
  \else\ifcsname faUser\endcsname\faUser\enspace\fi\fi}
\newcommand{\iconCal}{\ifcsname faCalendarAlt\endcsname\faCalendarAlt\enspace
  \else\ifcsname faCalendarDays\endcsname\faCalendarDays\enspace
  \else\ifcsname faCalendar\endcsname\faCalendar\enspace\fi\fi\fi}
\newcommand{\iconRight}{\ifcsname faChevronRight\endcsname\faChevronRight\else$\triangleright$\fi}
"""


# ---------------------------------------------------------------------------
# Преамбула: титульный лист \makelecturetitle{Курс}{Семестр}{Лекция}{Тема}
# ---------------------------------------------------------------------------

_TITLEPAGE_COLORFUL = r"""
% ============================================================
%   ТИТУЛЬНЫЙ ЛИСТ: \makelecturetitle{Курс}{Семестр}{Лекция}{Тема}
% ============================================================
\newif\ifuniversumtikz
\IfFileExists{tikz.sty}{\universumtikztrue}{\universumtikzfalse}
\ifuniversumtikz
  \newcommand{\makelecturetitle}[4]{%
  \begin{titlepage}
  \centering
  \begin{tikzpicture}[remember picture, overlay]
    \fill[primary] (current page.north west) rectangle
      ([yshift=-5cm]current page.north east);
    \fill[secondary] ([yshift=-5cm]current page.north west) rectangle
      ([yshift=-5.3cm]current page.north east);
    \fill[accent] ([yshift=-5.3cm]current page.north west) rectangle
      ([yshift=-5.45cm]current page.north east);
    \fill[white, opacity=0.08]
      ([xshift=2cm, yshift=-2cm]current page.north east) circle (4cm);
    \fill[white, opacity=0.06]
      ([xshift=14cm, yshift=-4cm]current page.north west) circle (2.5cm);
  \end{tikzpicture}
  \vspace*{0.5cm}
  {\color{white}\Huge\bfseries\sffamily #1\par}
  \vspace{0.3cm}
  {\color{white}\Large\sffamily #2\par}
  \vspace*{4cm}
  {\color{primary}\Huge\bfseries #3\par}
  \vspace{0.5cm}
  {\color{secondary}\Large\sffamily #4\par}
  \vfill
  {\color{textgray}\large\iconUser Лектор: \textbf{Фамилия И.О.}\par}
  \vspace{0.3cm}
  {\color{textgray}\large\iconCal \today\par}
  \vspace{1cm}
  \end{titlepage}
  }
\else
  \newcommand{\makelecturetitle}[4]{%
  \begin{titlepage}
  \centering
  {\color{primary}\rule{\linewidth}{3pt}}\par
  \vspace{1.2cm}
  {\Huge\bfseries\sffamily #1\par}
  \vspace{0.4cm}
  {\Large\sffamily #2\par}
  \vspace{2.5cm}
  {\color{primary}\Huge\bfseries #3\par}
  \vspace{0.5cm}
  {\color{secondary}\Large\sffamily #4\par}
  \vfill
  {\large \today\par}
  \vspace{1cm}
  \end{titlepage}
  }
\fi
"""

_TITLEPAGE_STRICT = r"""
% ============================================================
%   ТИТУЛЬНЫЙ ЛИСТ: \makelecturetitle{Курс}{Семестр}{Лекция}{Тема}
% ============================================================
\newcommand{\makelecturetitle}[4]{%
\begin{titlepage}
\centering
{\color{primary}\rule{\linewidth}{2pt}}\par
\vspace{1.5cm}
{\Huge\bfseries\sffamily #3\par}
\vspace{0.6cm}
{\Large\sffamily #4\par}
\vspace{2.5cm}
{\Large #1\par}
\vspace{0.3cm}
{\large #2\par}
\vspace{0.5cm}
{\large \today\par}
\vfill
{\color{primary}\rule{\linewidth}{0.6pt}}
\end{titlepage}
}
"""

_TITLEPAGE_PLAIN = r"""
% ============================================================
%   ТИТУЛЬНЫЙ ЛИСТ: \makelecturetitle{Курс}{Семестр}{Лекция}{Тема}
% ============================================================
\newcommand{\makelecturetitle}[4]{%
\begin{titlepage}
\centering
\vspace*{3cm}
{\LARGE\bfseries #3\par}
\vspace{0.8cm}
{\Large #4\par}
\vspace{2cm}
{\large #1\par}
\vspace{0.3cm}
{\normalsize #2\par}
\vfill
{\normalsize \today\par}
\vspace{1cm}
\end{titlepage}
}
"""


# ---------------------------------------------------------------------------
# Преамбула: заголовки, списки, колонтитулы
# ---------------------------------------------------------------------------

_TITLES_COLORFUL = r"""
% ---------- заголовки ----------
\titleformat{\section}
  {\Large\bfseries\sffamily\color{primary}}
  {\thesection.}{0.6em}{}
  [\vspace{-0.4em}{\color{secondary}\rule{\linewidth}{1.2pt}}]
\titleformat{\subsection}
  {\large\bfseries\sffamily\color{primary}}
  {\color{secondary}\thesubsection}{0.5em}{}
\titleformat{\subsubsection}
  {\normalsize\bfseries\sffamily\color{accent}}
  {\thesubsubsection}{0.5em}{}
\titlespacing*{\section}{0pt}{1.6em}{0.9em}
\titlespacing*{\subsection}{0pt}{1.2em}{0.5em}

% ---------- списки ----------
\setlist[itemize]{label={\color{secondary}\iconRight}, leftmargin=1.4em, itemsep=2pt}
\setlist[enumerate]{label={\color{primary}\bfseries\arabic*.}, leftmargin=1.6em, itemsep=2pt}
"""

_TITLES_STRICT = r"""
% ---------- заголовки ----------
\titleformat{\section}
  {\Large\bfseries\sffamily}
  {\thesection.}{0.6em}{}
  [\vspace{-0.4em}{\color{primary!60}\rule{\linewidth}{0.6pt}}]
\titleformat{\subsection}{\large\bfseries\sffamily}{\thesubsection}{0.5em}{}
\titleformat{\subsubsection}{\normalsize\bfseries\sffamily}{\thesubsubsection}{0.5em}{}
\titlespacing*{\section}{0pt}{1.5em}{0.8em}
\titlespacing*{\subsection}{0pt}{1.1em}{0.5em}

% ---------- списки ----------
\setlist[itemize]{leftmargin=1.4em, itemsep=2pt}
\setlist[enumerate]{leftmargin=1.6em, itemsep=2pt}
"""

_TITLES_PLAIN = r"""
% ---------- списки ----------
\setlist[itemize]{leftmargin=1.4em, itemsep=2pt}
\setlist[enumerate]{leftmargin=1.6em, itemsep=2pt}
"""

_HEADERS_COLORFUL = r"""
% ---------- колонтитулы ----------
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0pt}
\fancyhead[L]{\footnotesize\color{primary}\iconGrad\ \textbf{Учебный материал}}
\fancyhead[R]{\footnotesize\color{primary}\thepage}
\fancyfoot[C]{\footnotesize\color{textgray}\textit{Universum}}
"""

_HEADERS_STRICT = r"""
% ---------- колонтитулы ----------
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0.4pt}
\fancyhead[L]{\footnotesize Учебный материал}
\fancyhead[R]{\footnotesize\thepage}
"""

_HEADERS_PLAIN = r"""
% ---------- колонтитулы ----------
\pagestyle{plain}
"""


def build_preamble(style: Style, language: str = "ru") -> str:
    """Собрать преамбулу для выбранного стиля.

    Преамбула одинаковая по набору возможностей во всех стилях: одни и те же
    окружения (``definition``/``theorem``/``example``/``important``/``keyidea``),
    команда ``\\makelecturetitle`` и вспомогательные ``\\term``/``\\hl``,
    различаются только цвета, иконки и насыщенность оформления.

    Args:
        style: Стиль оформления.
        language: Язык документа ('ru' — русский основной, иначе английский).
            В babel основной язык задаётся последней опцией.

    Returns:
        Полная преамбула (от ``\\documentclass`` до последней команды перед
        ``\\begin{document}``), начинающаяся магической строкой.
    """
    palette = _PALETTES.get(style, _PALETTES[Style.STRICT])
    common = _PREAMBLE_COMMON
    for name, value in palette.items():
        common = common.replace(f"@{name.upper()}@", value)
    babel_opts = "english,russian" if (language or "ru").startswith("ru") else "russian,english"
    common = common.replace("@BABELOPTS@", babel_opts)

    if style == Style.COLORFUL:
        # иконки только в красочном стиле
        icons = _ICONS_LOAD + _ICONS_MACROS
        boxes, titlepage = _BOXES_COLORFUL, _TITLEPAGE_COLORFUL
        titles, headers = _TITLES_COLORFUL, _HEADERS_COLORFUL
    elif style == Style.STRICT:
        icons = _ICONS_MACROS
        boxes, titlepage = _BOXES_STRICT, _TITLEPAGE_STRICT
        titles, headers = _TITLES_STRICT, _HEADERS_STRICT
    else:  # Style.PLAIN — блоки без рамок, вызываются сразу
        icons = _ICONS_MACROS
        boxes, titlepage = _PLAIN_BOXES + "\n\\universumplainboxes\n", _TITLEPAGE_PLAIN
        titles, headers = _TITLES_PLAIN, _HEADERS_PLAIN

    return "\n".join(
        [
            MAGIC_COMMENT,
            r"\documentclass[11pt,a4paper]{article}",
            common,
            icons,
            _PLAIN_BOXES,
            boxes,
            titlepage,
            titles,
            headers,
            _COMMON_TAIL,
        ]
    )


def wrap_document(body: str, style: Style = Style.STRICT, language: str = "ru") -> str:
    """Собрать полный компилируемый документ из тела и преамбулы стиля.

    Args:
        body: Тело документа (то, что сгенерировала модель) — без преамбулы.
        style: Стиль оформления.
        language: Язык документа ('ru' или 'en').

    Returns:
        Полный LaTeX-документ: магическая строка, преамбула,
        ``\\begin{document}``, тело, ``\\end{document}``.
    """
    text = "" if body is None else str(body).strip()
    return "\n".join(
        [
            build_preamble(style, language),
            r"\begin{document}",
            "",
            text,
            "",
            r"\end{document}",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# Системный промт
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = r"""Ты — опытный преподаватель и эксперт по LaTeX. Ты создаёшь учебный документ для приложения Universum.

ФОРМАТ ОТВЕТА:
1. Верни ТОЛЬКО тело документа — то, что должно стоять между \begin{document} и \end{document}. Никаких пояснений до/после.
2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать \documentclass, \usepackage, \begin{document}, \end{document}, определения команд и любые изменения преамбулы. Преамбула уже готова и жёстко зафиксирована в приложении; если ты её продублируешь или изменишь — документ не соберётся.
3. Первая строка тела — титульный лист командой \makelecturetitle{Курс}{Семестр}{Лекция}{Тема} (4 аргумента в фигурных скобках, заполни их по смыслу запроса). Никаких самодельных титульных страниц, \title, \maketitle, \begin{titlepage}.
4. Допустимо обернуть ответ в ```latex ... ```.

ДОСТУПНЫЕ КОМАНДЫ И ОКРУЖЕНИЯ (только они; всё остальное уже подключено в преамбуле):
- Структура: \section{...}, \subsection{...}, \subsubsection{...}, \paragraph{...}.
- Блоки-акценты (обязательно используй их, а не просто жирный текст):
  \begin{definition}...\end{definition} — определения и термины;
  \begin{theorem}...\end{theorem} — теоремы, законы, правила;
  \begin{example}...\end{example} — примеры и разборы задач;
  \begin{important}...\end{important} — предупреждения, типичные ошибки, «не путайте»;
  \begin{keyidea}...\end{keyidea} — ключевая мысль раздела/лекции.
  В каждом крупном разделе — минимум 1–2 таких блока. Не вкладывай блоки друг в друга.
- Выделение: \term{термин} (ключевой термин), \hl{текст} (маркером), \textbf{}, \textit{}, \emph{}.
- Списки: itemize, enumerate (можно с [label=...]).
- Формулы: $...$ в строке, \[ ... \] и окружения equation/align для выносных.
- Таблицы: tabular / tabularx / booktabs-стиль (\toprule/\midrule/\bottomrule), окружение table с \caption.
- Код: lstlisting с параметром language (Python, Java, C, SQL и т.д.). НЕ minted.
- Ссылки: \url{...} и \href{...}{...} для внешних адресов; \label/\ref внутри документа.
- Доказательства: окружение proof и символ \qed (пакет amsthm уже подключён) — ставь \qed в конце доказательства или используй \begin{proof}...\end{proof}.

ЖЁСТКИЕ ПРАВИЛА (нарушение = брак):
1. Используй только перечисленные выше команды и окружения. Запрещены любые \usepackage и подключение пакетов, minted, tikz-рисунки, \includegraphics, \input/\include, biblatex/biber, glossaries, cleveref, siunitx.
2. НИКОГДА не используй символ @ в командах LaTeX и внутренние макросы ядра: \@, \@@, \@title, \@date, \@author, \@startsection, \makeatletter, \makeatother, \spacefactor. Это фатальная ошибка «You can't use \spacefactor in internal vertical mode».
3. Закрывай все окружения: каждый \begin{...} имеет парный \end{...}. Не оставляй незакрытых скобок {} и $.
4. Экранируй спецсимволы в обычном тексте: % & $ # _ { } ~ ^ \ . Внутри формул и листингов — не экранируй.
5. Кириллица и типографика уже настроены. Пиши русский текст обычными Unicode-буквами, без транслита и без \CYR-команд.
6. Язык документа = язык запроса пользователя (русский или английский). Заголовки, подписи — на этом языке.
7. Не оставляй плейсхолдеров вида [вставить сюда] и «TODO». Каждый раздел — с реальным содержательным текстом.
8. Не выдумывай источники и DOI: только реальные, широко известные издания; при сомнении — без URL.
"""


# ---------------------------------------------------------------------------
# Блоки стилей (влияют на акценты в тексте; оформление задаёт преамбула)
# ---------------------------------------------------------------------------

STYLE_STRICT = r"""СТИЛЬ: СТРОГИЙ (академический).
Оформление уже задано преамбулой: чёрно-белая гамма, строгие рамки блоков, без иконок и цветных плашек.
Что делать тебе: академический тон, чёткая нумерация разделов, определения и теоремы — в блоках definition/theorem, примеры — в example, предупреждения — в important, итог раздела — в keyidea. Никаких восклицаний, эмодзи и «воды».
НИКОГДА не используй \@, \makeatletter, \makeatother и другие внутренние макросы — титул создаётся только \makelecturetitle."""

STYLE_COLORFUL = r"""СТИЛЬ: КРАСОЧНЫЙ / КРЕАТИВНЫЙ.
Оформление уже задано преамбулой: цветные блоки (definition — бирюза, theorem — синий, example и important — оранжевый, keyidea — плашка с акцентом), цветные заголовки, титульный лист с плашкой.
Что делать тебе: структурируй материал визуально — чаще используй блоки (в каждом разделе минимум 1–2), выделяй термины \term{...} и ключевые формулы, важные выводы оформляй в keyidea, типичные ошибки — в important. Цвет и блоки — акцент, а не фон: текст остаётся читаемым и академичным.
НИКОГДА не используй \@, \makeatletter, \makeatother и другие внутренние макросы — титул создаётся только \makelecturetitle."""

STYLE_PLAIN = r"""СТИЛЬ: БЕЗ СТИЛЯ (минимум оформления).
Оформление уже задано преамбулой: обычный чёрно-белый текст, блоки-акценты без рамок (простые абзацы с заголовком), без иконок и цветов.
Что делать тебе: простой линейный текст — section/subsection, обычные абзацы и списки. Блоки definition/theorem/example/important/keyidea использовать можно, но умеренно (1–2 на раздел), без декоративных вставок.
НИКОГДА не используй \@, \makeatletter, \makeatother и другие внутренние макросы — титул создаётся только \makelecturetitle."""

STYLE_BLOCKS: dict[Style, str] = {
    Style.STRICT: STYLE_STRICT,
    Style.COLORFUL: STYLE_COLORFUL,
    Style.PLAIN: STYLE_PLAIN,
}


# ---------------------------------------------------------------------------
# Блоки типов документов
# ---------------------------------------------------------------------------

DOCTYPE_LECTURE = r"""ТИП ДОКУМЕНТА: ЛЕКЦИЯ.
Структура (строго в этом порядке):
1. Титульный лист: \makelecturetitle{Курс}{Семестр / Модуль}{Лекция N}{Тема лекции}. Заполни 4 аргумента по смыслу запроса.
2. \tableofcontents (если документ длинный).
3. Введение (\section{Введение}): актуальность, цели и задачи, что студент будет знать/уметь после.
4. Основные разделы: 3–5 логических разделов (или по структуре пользователя). В каждом — теория, минимум один блок definition/theorem, пример в example.
5. Примеры/кейсы: 1–2 развёрнутых примера с пошаговым разбором в блоке example.
6. Выводы (\section{Выводы}): 4–7 пунктов + финальный keyidea.
7. Литература (\section*{Литература}): 3–6 реальных источников.
Требования: академический тон, определения терминов, формулы с пояснениями."""

DOCTYPE_SUMMARY = r"""ТИП ДОКУМЕНТА: КОНСПЕКТ.
Структура (сжатая, без воды):
1. Титульный лист: \makelecturetitle{Курс}{Семестр / Модуль}{Конспект}{Тема}.
2. Краткое вступление (2–4 предложения).
3. \section{Ключевые тезисы}: маркированный список главных идей.
4. \section{Определения}: термины — каждый в блоке definition (или списком, если их много).
5. \section{Формулы}: ключевые формулы с расшифровкой символов.
6. \section{Разбор темы}: 2–4 коротких раздела с примерами в example.
7. \section*{Итог}: keyidea + таблица «самое важное за 1 страницу».
Требования: максимальная плотность, минимум воды, блоки — короткие и по делу."""

DOCTYPE_HOMEWORK = r"""ТИП ДОКУМЕНТА: ДОМАШНЕЕ ЗАДАНИЕ.
Структура:
1. Титульный лист: \makelecturetitle{Курс}{Семестр / Модуль}{Домашнее задание}{Тема}.
2. Инструкция: срок, формат сдачи, что оценивается (если не задано — разумные значения).
3. Задания по уровням: базовый (2–3), стандартный (2–3), повышенный (1–2). Каждое задание — с чёткой формулировкой, данными, ожидаемым результатом; нумерация обязательна.
4. Требования к оформлению работы (1 абзац).
5. Критерии оценивания — таблица «критерий — баллы».
ВАЖНО: не давай решений и не раскрывай ответы. Определения и теоремы, нужные для решения, оформи в definition/theorem (это не ответы). Где полезно — подсказка в important."""

DOCTYPE_TEST = r"""ТИП ДОКУМЕНТА: ТЕСТ.
Структура:
1. Титульный лист: \makelecturetitle{Курс}{Семестр / Модуль}{Тест}{Тема}.
2. Инструкции: число вопросов, типы, как отвечать, время.
3. Вопросы (\section{Вопросы}): если не указано иное — микс: ~60% с одним верным ответом, ~25% с множественным выбором, ~15% открытые. 4 варианта (а, б, в, г); для множественного выбора 4–5 вариантов и явная пометка. Каждый вопрос — отдельным абзацем, варианты — списком enumerate с буквами.
4. Ключ ответов (\section{Ключ ответов}): таблица «№ — правильный ответ» + эталонные краткие ответы для открытых вопросов.
Требования: вопросы строго по теме; дистракторы правдоподобные, но неверные; никакой двусмысленности."""

DOCTYPE_BLOCKS: dict[DocType, str] = {
    DocType.LECTURE: DOCTYPE_LECTURE,
    DocType.SUMMARY: DOCTYPE_SUMMARY,
    DocType.HOMEWORK: DOCTYPE_HOMEWORK,
    DocType.TEST: DOCTYPE_TEST,
}


# ---------------------------------------------------------------------------
# Промт авто-ремонта
# ---------------------------------------------------------------------------

REPAIR_PROMPT_TEMPLATE = r"""Ты — эксперт по LaTeX. Ниже — тело LaTeX-документа (без преамбулы), которое не скомпилировалось, и лог ошибки компилятора.

Твоя задача: исправь тело так, чтобы оно скомпилировалось БЕЗ ошибок. Верни ТОЛЬКО исправленное тело (без пояснений, допустимо в ```latex ... ```).

Правила исправления:
- Преамбула фиксирована приложением: НЕ добавляй \documentclass, \usepackage, \begin{document}, \end{document}, определения команд. Если они есть в ответе — удали.
- Исправляй только то, что ломает компиляцию. Не меняй содержание, структуру и стиль без необходимости.
- Частые причины: незакрытые окружения (\begin/\end), незакрытые скобки, неэкранированные спецсимволы (& % $ # _), неизвестные команды, ошибки в lstlisting, неверный синтаксис таблиц.
- Если в логе «You can't use \spacefactor in internal vertical mode», «Undefined control sequence \@» или упоминаются \makeatletter/\makeatother — удали ВСЕ \@-конструкции, а титул собери только через \makelecturetitle{Курс}{Семестр}{Лекция}{Тема}.
- Если в логе «Environment X undefined» — используй только разрешённые окружения: definition, theorem, example, important, keyidea.
- Если в логе ошибка кириллицы или шрифтов — не меняй кодировки, а замени проблемный символ обычной буквой.
- Сохрани \makelecturetitle в начале и всё содержание.

--- ЛОГ ОШИБКИ КОМПИЛЯТОРА ---
{error_log}
--- КОНЕЦ ЛОГА ---

--- ИСХОДНОЕ ТЕЛО ДОКУМЕНТА ---
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
    """Собрать полный промт генерации тела LaTeX-документа.

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
        "",
        r"НАПОМИНАНИЕ: верни ТОЛЬКО тело (без \documentclass, без \usepackage, без \begin{document}/\end{document}). "
        r"Первая строка тела — \makelecturetitle{Курс}{Семестр}{Лекция}{Тема}.",
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
            r"АКТУАЛЬНЫЕ ДАННЫЕ ИЗ ИНТЕРНЕТА (используй их, вплети в текст естественно; в конце документа добавь раздел «Источники» / «Sources» со списком ниже, каждый пункт — название + \url{ссылка} + дата актуальности):",
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
        r"Начни ответ сразу с \makelecturetitle. Никакого текста перед ним.",
    ]
    return "\n".join(parts)


def build_repair_prompt(latex_code: str, error_log: str) -> str:
    """Собрать промт авто-ремонта LaTeX.

    Args:
        latex_code: Нескомпилировавшееся тело документа (или весь документ —
            приложение само извлечёт тело).
        error_log: Лог компилятора (обрезается до 8000 символов).

    Returns:
        Промт для LLM.
    """
    # error_log может прийти как None (движок без вывода) — не падаем на срезе.
    # Используем replace, а не format: в шаблоне есть LaTeX-скобки (\begin{document}).
    safe_log = "" if error_log is None else str(error_log)
    safe_code = "" if latex_code is None else str(latex_code)
    return (
        REPAIR_PROMPT_TEMPLATE
        .replace("{error_log}", safe_log[:8000] or "(лог компилятора пуст)")
        .replace("{latex_code}", safe_code[:60000])
    )
