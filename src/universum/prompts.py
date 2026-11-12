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
    Style.STRICT: {
        "primary": "000000",
        "secondary": "333333",
        "accent": "555555",
        "soft": "F7F7F7",
        "highlight": "E6E6E6",
        "textgray": "333333",
    },
    Style.COLORFUL: {
        "primary": "2E3192",    # глубокий синий
        "secondary": "F15A24",  # оранжевый акцент
        "accent": "00B4A6",     # бирюзовый
        "soft": "F4F6FB",       # нежный фон
        "highlight": "FFD166",  # жёлтый маркер
        "textgray": "3A3A3A",
    },
    Style.NEON: {
        "primary": "6D28D9",    # электрический фиолетовый
        "secondary": "DB2777",  # неоновый розовый
        "accent": "0891B2",     # неоновый циан
        "soft": "F5F3FF",       # светло-сиреневый фон блоков
        "highlight": "BEF264",  # кислотный лайм
        "textgray": "374151",
    },
    Style.NATURE: {
        "primary": "166534",    # лесной зелёный
        "secondary": "B45309",  # тёплая охра
        "accent": "0F766E",     # хвойный бирюзовый
        "soft": "F0FDF4",       # зелёная дымка
        "highlight": "FDE68A",  # песочный маркер
        "textgray": "1F2937",
    },
    Style.MINIMAL: {
        "primary": "111827",    # графит
        "secondary": "2563EB",  # единственный синий акцент
        "accent": "059669",     # зелёный акцент для примеров
        "soft": "F9FAFB",       # почти белый фон блоков
        "highlight": "E5E7EB",  # светло-серый маркер
        "textgray": "4B5563",
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
\usepackage{etoolbox}   % \ifstrempty для необязательных данных титула
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

% ---------- тип документа задаёт приложение, а не модель ----------
% Значение подставляет build_preamble() из выбора пользователя.
\newcommand{\universumdoctype}{@DOCTYPE@}
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

# Неон: яркие плашки, толстые «светящиеся» рамки, круглые углы
_BOXES_NEON = r"""
% ============================================================
%   БЛОКИ «НЕОН» (tcolorbox) + запасной вариант без tcolorbox
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
      boxrule=1pt, arc=9pt, outer arc=9pt,
      left=10pt, right=10pt, top=8pt, bottom=8pt,
      attach boxed title to top left={xshift=8pt, yshift=-9pt},
      boxed title style={colback=primary, sharp corners, boxrule=0pt, arc=3pt,
        left=6pt, right=6pt, top=3pt, bottom=3pt},
      top=15pt,
      borderline={0.7pt}{-2pt}{accent}
    }
  }
  \newtcolorbox{definition}[1][]{lecbox, title={\iconBulb Определение},
    colframe=accent, boxed title style={colback=accent}, #1}
  \newtcolorbox{theorem}[1][]{lecbox, title={\iconBook Теорема}, #1}
  \newtcolorbox{example}[1][]{lecbox, title={\iconFlask Пример},
    colframe=secondary, boxed title style={colback=secondary}, #1}
  \newtcolorbox{important}[1][]{lecbox, title={\iconWarn Важно},
    colframe=secondary, boxed title style={colback=secondary},
    colback=highlight!20, #1}
  \newtcolorbox{keyidea}[1][]{enhanced, breakable, colback=primary!10, colframe=primary,
    boxrule=1.4pt, arc=10pt, left=12pt, right=12pt, top=10pt, bottom=10pt,
    fontupper=\itshape, borderline west={4pt}{0pt}{secondary}, #1}
\else
  \universumplainboxes
\fi
"""

# Природа: мягкие зелёные плашки с крупными скруглениями
_BOXES_NATURE = r"""
% ============================================================
%   БЛОКИ «ПРИРОДА» (tcolorbox) + запасной вариант без tcolorbox
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
      boxrule=0.9pt, arc=12pt, outer arc=12pt,
      left=11pt, right=11pt, top=8pt, bottom=8pt,
      attach boxed title to top left={xshift=9pt, yshift=-9pt},
      boxed title style={colback=primary, sharp corners, boxrule=0pt, arc=12pt,
        left=7pt, right=7pt, top=3pt, bottom=3pt},
      top=15pt
    }
  }
  \newtcolorbox{definition}[1][]{lecbox, title={\iconBulb Определение},
    colframe=accent, boxed title style={colback=accent}, #1}
  \newtcolorbox{theorem}[1][]{lecbox, title={\iconBook Теорема}, #1}
  \newtcolorbox{example}[1][]{lecbox, title={\iconFlask Пример},
    colframe=secondary, boxed title style={colback=secondary}, #1}
  \newtcolorbox{important}[1][]{lecbox, title={\iconWarn Важно},
    colframe=secondary, boxed title style={colback=secondary},
    colback=highlight!30, #1}
  \newtcolorbox{keyidea}[1][]{enhanced, breakable, colback=primary!7,
    colframe=primary!70, boxrule=1pt, arc=12pt,
    left=12pt, right=12pt, top=10pt, bottom=10pt,
    fontupper=\itshape, borderline west={4pt}{0pt}{accent}, #1}
\else
  \universumplainboxes
\fi
"""

# Минимализм: без плашек-заголовков, тонкая цветная линия слева
_BOXES_MINIMAL = r"""
% ============================================================
%   БЛОКИ «МИНИМАЛИЗМ» (tcolorbox) + запасной вариант без tcolorbox
% ============================================================
\newif\ifuniversumfancy
\IfFileExists{tcolorbox.sty}{\universumfancytrue}{\universumfancyfalse}
\ifuniversumfancy
  \usepackage[most]{tcolorbox}
  \tcbset{
    lecbox/.style={
      enhanced, breakable,
      frame hidden, colback=white,
      colbacktitle=soft, coltitle=primary,
      fonttitle=\sffamily\bfseries\small,
      titlerule=0pt, boxrule=0pt, arc=2pt,
      left=12pt, right=8pt, top=6pt, bottom=6pt,
      borderline west={3pt}{0pt}{secondary}
    }
  }
  \newtcolorbox{definition}[1][]{lecbox, title={Определение},
    borderline west={3pt}{0pt}{accent}, #1}
  \newtcolorbox{theorem}[1][]{lecbox, title={Теорема}, #1}
  \newtcolorbox{example}[1][]{lecbox, title={Пример},
    borderline west={3pt}{0pt}{accent}, #1}
  \newtcolorbox{important}[1][]{lecbox, title={Важно},
    borderline west={3pt}{0pt}{secondary}, colback=highlight!25, #1}
  \newtcolorbox{keyidea}[1][]{lecbox, title={Ключевая мысль},
    fontupper=\itshape, colback=soft, borderline west={3pt}{0pt}{primary}, #1}
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

# Титул «с плашкой» — для насыщенных стилей (красочный, неон, природа).
# Тип документа подставляется приложением (\universumdoctype), модель задаёт
# только тему и — при желании — явно указанные пользователем доп. данные.
_TITLEPAGE_BANNER = r"""
% ============================================================
%   ТИТУЛЬНЫЙ ЛИСТ: \makelecturetitle[доп. данные]{Тема}
%   Тип документа — \universumdoctype (подставляет приложение)
% ============================================================
\newif\ifuniversumtikz
\IfFileExists{tikz.sty}{\universumtikztrue}{\universumtikzfalse}
\ifuniversumtikz
  \newcommand{\makelecturetitle}[2][]{%
  \begin{titlepage}
  \centering
  \begin{tikzpicture}[remember picture, overlay]
    \fill[primary] (current page.north west) rectangle
      ([yshift=-5cm]current page.north east);
    \fill[secondary] ([yshift=-5cm]current page.north west) rectangle
      ([yshift=-5.25cm]current page.north east);
    \fill[accent] ([yshift=-5.25cm]current page.north west) rectangle
      ([yshift=-5.38cm]current page.north east);
    \fill[white, opacity=0.08]
      ([xshift=2cm, yshift=-2cm]current page.north east) circle (4cm);
    \fill[white, opacity=0.06]
      ([xshift=14cm, yshift=-4cm]current page.north west) circle (2.5cm);
  \end{tikzpicture}%
  \vspace*{1.1cm}
  {\color{white}\sffamily\Large\bfseries\universumdoctype\par}
  \vspace{2.1cm}
  {\color{primary}\Huge\bfseries #2\par}
  \vspace{0.5cm}
  {\color{secondary}\rule{6cm}{1.2pt}\par}
  \ifstrempty{#1}{}{%
    \vspace{0.6cm}%
    {\color{textgray}\large\sffamily #1\par}%
  }%
  \vfill
  {\color{textgray}\large\iconCal \today\par}
  \vspace{1cm}
  \end{titlepage}
  }
\else
  \newcommand{\makelecturetitle}[2][]{%
  \begin{titlepage}
  \centering
  {\color{primary}\rule{\linewidth}{3pt}}\par
  \vspace{1.6cm}
  {\color{primary}\sffamily\Large\bfseries\universumdoctype\par}
  \vspace{1.8cm}
  {\Huge\bfseries #2\par}
  \vspace{0.5cm}
  {\color{secondary}\rule{6cm}{1.2pt}\par}
  \ifstrempty{#1}{}{%
    \vspace{0.6cm}%
    {\color{textgray}\large\sffamily #1\par}%
  }%
  \vfill
  {\color{textgray}\large\iconCal \today\par}
  \vspace{1cm}
  \end{titlepage}
  }
\fi
"""

# Титул «линейками» — для строгого и минималистичного стилей
_TITLEPAGE_FRAME = r"""
% ============================================================
%   ТИТУЛЬНЫЙ ЛИСТ: \makelecturetitle[доп. данные]{Тема}
%   Тип документа — \universumdoctype (подставляет приложение)
% ============================================================
\newcommand{\makelecturetitle}[2][]{%
\begin{titlepage}
\centering
{\color{primary}\rule{\linewidth}{2pt}}\par
\vspace{1.6cm}
{\Huge\bfseries\sffamily #2\par}
\vspace{0.6cm}
{\color{secondary}\rule{5cm}{0.8pt}}\par
\vspace{0.7cm}
{\Large\sffamily\universumdoctype\par}
\ifstrempty{#1}{}{%
  \vspace{0.6cm}%
  {\normalsize\color{textgray} #1\par}%
}
\vfill
{\large \today\par}
\vspace{0.6cm}
{\color{primary}\rule{\linewidth}{0.6pt}}
\end{titlepage}
}
"""


# ---------------------------------------------------------------------------
# Преамбула: заголовки, списки, колонтитулы
# ---------------------------------------------------------------------------

_TITLES_NEON = r"""
% ---------- заголовки ----------
\titleformat{\section}
  {\Large\bfseries\sffamily\color{primary}}
  {\thesection.}{0.6em}{}
  [\vspace{-0.4em}{\color{secondary}\rule{\linewidth}{1.6pt}}]
\titleformat{\subsection}
  {\large\bfseries\sffamily\color{accent}}
  {\color{secondary}\thesubsection}{0.5em}{}
\titleformat{\subsubsection}
  {\normalsize\bfseries\sffamily\color{primary}}
  {\thesubsubsection}{0.5em}{}
\titlespacing*{\section}{0pt}{1.6em}{0.9em}
\titlespacing*{\subsection}{0pt}{1.2em}{0.5em}

% ---------- списки ----------
\setlist[itemize]{label={\color{secondary}\iconRight}, leftmargin=1.4em, itemsep=2pt}
\setlist[enumerate]{label={\color{primary}\bfseries\arabic*.}, leftmargin=1.6em, itemsep=2pt}
\setlist[enumerate,2]{label={\alph*)}, leftmargin=1.6em, itemsep=1pt}
"""

_TITLES_NATURE = r"""
% ---------- заголовки ----------
\titleformat{\section}
  {\Large\bfseries\sffamily\color{primary}}
  {\thesection.}{0.6em}{}
  [\vspace{-0.4em}{\color{accent}\rule{\linewidth}{1.2pt}}]
\titleformat{\subsection}
  {\large\bfseries\sffamily\color{accent}}
  {\color{primary}\thesubsection}{0.5em}{}
\titleformat{\subsubsection}
  {\normalsize\bfseries\sffamily\color{secondary}}
  {\thesubsubsection}{0.5em}{}
\titlespacing*{\section}{0pt}{1.6em}{0.9em}
\titlespacing*{\subsection}{0pt}{1.2em}{0.5em}

% ---------- списки ----------
\setlist[itemize]{label={\color{accent}\iconRight}, leftmargin=1.4em, itemsep=3pt}
\setlist[enumerate]{label={\color{primary}\bfseries\arabic*.}, leftmargin=1.6em, itemsep=3pt}
\setlist[enumerate,2]{label={\alph*)}, leftmargin=1.6em, itemsep=1pt}
"""

_TITLES_MINIMAL = r"""
% ---------- заголовки ----------
\titleformat{\section}
  {\Large\bfseries\sffamily\color{primary}}
  {\thesection}{0.6em}{}
\titleformat{\subsection}
  {\large\bfseries\sffamily\color{primary}}
  {\thesubsection}{0.5em}{}
\titleformat{\subsubsection}
  {\normalsize\sffamily\color{secondary}}
  {\thesubsubsection}{0.5em}{}
\titlespacing*{\section}{0pt}{1.4em}{0.7em}
\titlespacing*{\subsection}{0pt}{1.0em}{0.4em}

% ---------- списки ----------
\setlist[itemize]{label={\color{secondary}--}, leftmargin=1.3em, itemsep=2pt}
\setlist[enumerate]{label={\color{secondary}\arabic*.}, leftmargin=1.5em, itemsep=2pt}
\setlist[enumerate,2]{label={\alph*)}, leftmargin=1.6em, itemsep=1pt}
"""

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
\setlist[enumerate,2]{label={\alph*)}, leftmargin=1.6em, itemsep=1pt}
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
\setlist[enumerate,2]{label={\alph*)}, leftmargin=1.6em, itemsep=1pt}
"""

_HEADERS_NEON = r"""
% ---------- колонтитулы ----------
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0.4pt}
\renewcommand{\headrule}{\hbox to\headwidth{\color{accent}\leaders\hrule height \headrulewidth\hfill}}
\fancyhead[L]{\footnotesize\color{primary}\sffamily\bfseries\universumdoctype}
\fancyhead[R]{\footnotesize\color{secondary}\sffamily\thepage}
\fancyfoot[C]{\footnotesize\color{textgray}\itshape Universum}
"""

_HEADERS_NATURE = r"""
% ---------- колонтитулы ----------
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0.4pt}
\renewcommand{\headrule}{\hbox to\headwidth{\color{primary!50}\leaders\hrule height \headrulewidth\hfill}}
\fancyhead[L]{\footnotesize\color{primary}\sffamily\iconGrad\ \universumdoctype}
\fancyhead[R]{\footnotesize\color{accent}\sffamily\thepage}
\fancyfoot[C]{\footnotesize\color{textgray}\itshape Universum}
"""

_HEADERS_MINIMAL = r"""
% ---------- колонтитулы ----------
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0.4pt}
\renewcommand{\headrule}{\hbox to\headwidth{\color{highlight}\leaders\hrule height \headrulewidth\hfill}}
\fancyhead[L]{\footnotesize\sffamily\color{secondary}\universumdoctype}
\fancyhead[R]{\footnotesize\sffamily\color{textgray}\thepage}
"""

_HEADERS_COLORFUL = r"""
% ---------- колонтитулы ----------
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0pt}
\fancyhead[L]{\footnotesize\color{primary}\iconGrad\ \textbf{\universumdoctype}}
\fancyhead[R]{\footnotesize\color{primary}\thepage}
\fancyfoot[C]{\footnotesize\color{textgray}\textit{Universum}}
"""

_HEADERS_STRICT = r"""
% ---------- колонтитулы ----------
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0.4pt}
\fancyhead[L]{\footnotesize\universumdoctype}
\fancyhead[R]{\footnotesize\thepage}
"""

# Составные части оформления по стилям: блоки, титул, заголовки, колонтитулы.
_STYLE_LAYOUT: dict[Style, dict[str, str]] = {
    Style.STRICT: {
        "boxes": "_BOXES_STRICT",
        "titlepage": "_TITLEPAGE_FRAME",
        "titles": "_TITLES_STRICT",
        "headers": "_HEADERS_STRICT",
        "icons": "_ICONS_MACROS",
    },
    Style.COLORFUL: {
        "boxes": "_BOXES_COLORFUL",
        "titlepage": "_TITLEPAGE_BANNER",
        "titles": "_TITLES_COLORFUL",
        "headers": "_HEADERS_COLORFUL",
        "icons": "_ICONS_LOAD + _ICONS_MACROS",
    },
    Style.NEON: {
        "boxes": "_BOXES_NEON",
        "titlepage": "_TITLEPAGE_BANNER",
        "titles": "_TITLES_NEON",
        "headers": "_HEADERS_NEON",
        "icons": "_ICONS_LOAD + _ICONS_MACROS",
    },
    Style.NATURE: {
        "boxes": "_BOXES_NATURE",
        "titlepage": "_TITLEPAGE_BANNER",
        "titles": "_TITLES_NATURE",
        "headers": "_HEADERS_NATURE",
        "icons": "_ICONS_LOAD + _ICONS_MACROS",
    },
    Style.MINIMAL: {
        "boxes": "_BOXES_MINIMAL",
        "titlepage": "_TITLEPAGE_FRAME",
        "titles": "_TITLES_MINIMAL",
        "headers": "_HEADERS_MINIMAL",
        "icons": "_ICONS_MACROS",
    },
}


def _latex_escape(text: str) -> str:
    """Экранировать спецсимволы LaTeX в короткой строке (название типа)."""
    replacements = {
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
    return "".join(replacements.get(ch, ch) for ch in text)


def build_preamble(style: Style, language: str = "ru", doc_type: DocType | None = None) -> str:
    """Собрать преамбулу для выбранного стиля.

    Преамбула одинаковая по набору возможностей во всех стилях: одни и те же
    окружения (``definition``/``theorem``/``example``/``important``/``keyidea``),
    команда ``\\makelecturetitle`` и вспомогательные ``\\term``/``\\hl``;
    различаются только цвета, иконки и насыщенность оформления.

    Args:
        style: Стиль оформления.
        language: Язык документа ('ru' — русский основной, иначе английский).
            В babel основной язык задаётся последней опцией.
        doc_type: Тип документа. Название жёстко подставляется в
            ``\\universumdoctype`` (титул и колонтитулы), поэтому модель не может
            написать «Лекция» вместо «Тест».

    Returns:
        Полная преамбула (от ``\\documentclass`` до последней команды перед
        ``\\begin{document}``), начинающаяся магической строкой.
    """
    layout = _STYLE_LAYOUT.get(style, _STYLE_LAYOUT[Style.STRICT])
    palette = _PALETTES.get(style, _PALETTES[Style.STRICT])
    common = _PREAMBLE_COMMON
    for name, value in palette.items():
        common = common.replace(f"@{name.upper()}@", value)
    babel_opts = "english,russian" if (language or "ru").startswith("ru") else "russian,english"
    common = common.replace("@BABELOPTS@", babel_opts)
    doc_label = _latex_escape(doc_type.label_ru) if doc_type is not None else "Учебный материал"
    common = common.replace("@DOCTYPE@", doc_label)

    globals_ = globals()
    boxes = globals_[layout["boxes"]]
    titlepage = globals_[layout["titlepage"]]
    titles = globals_[layout["titles"]]
    headers = globals_[layout["headers"]]
    icons = "".join(globals_[name.strip()] for name in layout["icons"].split("+"))

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


def wrap_document(
    body: str,
    style: Style = Style.STRICT,
    language: str = "ru",
    doc_type: DocType | None = None,
) -> str:
    """Собрать полный компилируемый документ из тела и преамбулы стиля.

    Args:
        body: Тело документа (то, что сгенерировала модель) — без преамбулы.
        style: Стиль оформления.
        language: Язык документа ('ru' или 'en').
        doc_type: Тип документа (для ``\\universumdoctype`` на титуле).

    Returns:
        Полный LaTeX-документ: магическая строка, преамбула,
        ``\\begin{document}``, тело, ``\\end{document}``.
    """
    text = "" if body is None else str(body).strip()
    return "\n".join(
        [
            build_preamble(style, language, doc_type=doc_type),
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
3. Первая строка тела — титульный лист командой \makelecturetitle{Тема}: ОДИН аргумент — тема документа. Доп. данные — только тем же необязательным аргументом в квадратных скобках, например \makelecturetitle[Курс: Экономика · Семестр: 2]{Тема}, и ТОЛЬКО если пользователь сам их указал в запросе или в файле. Старый вид с четырьмя аргументами (\makelecturetitle{Курс}{Семестр}{Лекция}{Тема}) запрещён. Название типа документа на титуле подставляет приложение — тебе его писать не нужно. Никаких самодельных титульных страниц, \title, \maketitle, \author, \date, \begin{titlepage}.
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
9. Не выдумывай служебные данные документа: семестр, модуль, номер лекции, ФИО лектора, кафедру, вуз, группу, дату сдачи, время теста. Любое такое поле выводи ТОЛЬКО если оно явно указано в запросе или в исходном файле. Пустой титул лучше, чем выдуманные данные.
10. Не подменяй тип документа: если пользователь выбрал тест — это тест, а не лекция; тип на титуле подставит приложение.
"""


# ---------------------------------------------------------------------------
# Блоки стилей (влияют на акценты в тексте; оформление задаёт преамбула)
# ---------------------------------------------------------------------------

_STYLE_RULES = r"""НИКОГДА не используй \@, \makeatletter, \makeatother и другие внутренние макросы — титул создаётся только \makelecturetitle."""

STYLE_STRICT = r"""СТИЛЬ: СТРОГИЙ (академический).
Оформление уже задано преамбулой: чёрно-белая гамма, строгие рамки блоков, без иконок и цветных плашек.
Что делать тебе: академический тон, чёткая нумерация разделов, определения и теоремы — в блоках definition/theorem, примеры — в example, предупреждения — в important, итог раздела — в keyidea. Никаких восклицаний, эмодзи и «воды».
НИКОГДА не используй \@, \makeatletter, \makeatother и другие внутренние макросы — титул создаётся только \makelecturetitle."""

STYLE_COLORFUL = r"""СТИЛЬ: КРАСОЧНЫЙ / КРЕАТИВНЫЙ.
Оформление уже задано преамбулой: цветные блоки (definition — бирюза, theorem — синий, example и important — оранжевый, keyidea — плашка с акцентом), цветные заголовки, титульный лист с плашкой.
Что делать тебе: структурируй материал визуально — чаще используй блоки (в каждом разделе минимум 1–2), выделяй термины \term{...} и ключевые формулы, важные выводы оформляй в keyidea, типичные ошибки — в important. Цвет и блоки — акцент, а не фон: текст остаётся читаемым и академичным.
НИКОГДА не используй \@, \makeatletter, \makeatother и другие внутренние макросы — титул создаётся только \makelecturetitle."""

STYLE_NEON = r"""СТИЛЬ: НЕОН (яркий, «цифровой»).
Оформление уже задано преамбулой: электрический фиолетовый как основной, неоновый розовый и циан как акценты, кислотный лайм для маркера; блоки definition/theorem/example/important — с плашками и светящейся двойной рамкой, keyidea — фиолетовая плашка с розовой линией; титул с цветной полосой.
Что делать тебе: держи текст коротким и «плотным» (абзацы по 2–4 предложения), каждую ключевую мысль выноси в block; активно используй \term{...} и \hl{...} для ярких акцентов, формулы — отдельными строками; в каждом крупном разделе 1–2 блока и один keyidea-вывод. Не превращай весь текст в выделения: цвет — редкий, но мощный акцент.
НИКОГДА не используй \@, \makeatletter, \makeatother и другие внутренние макросы — титул создаётся только \makelecturetitle."""

STYLE_NATURE = r"""СТИЛЬ: ПРИРОДА (спокойный, «эко»).
Оформление уже задано преамбулой: лесной зелёный как основной, тёплая охра и хвойный бирюзовый как акценты, песочный маркер; блоки — с крупными скруглениями и мягкой зелёной заливкой, keyidea — зелёная плашка с бирюзовой линией; титул с зелёной полосой.
Что делать тебе: плавное, спокойное изложение с понятными переходами; определения — в definition, законы и правила — в theorem, разборы и жизненные аналогии — в example, ошибки — в important; в конце крупных разделов — keyidea. Используй \term{...} для терминов и \hl{...} для главного, нумерованные списки — для шагов и алгоритмов.
НИКОГДА не используй \@, \makeatletter, \makeatother и другие внутренние макросы — титул создаётся только \makelecturetitle."""

STYLE_MINIMAL = r"""СТИЛЬ: СОВРЕМЕННЫЙ МИНИМАЛИЗМ.
Оформление уже задано преамбулой: графитовый текст, один синий акцент, тонкие цветные линии слева у блоков, никаких цветных плашек и тяжёлых рамок, много воздуха; титул с линейками.
Что делать тебе: максимально чистая структура — короткие заголовки разделов, короткие абзацы, аккуратные списки; блоки definition/theorem/example/important/keyidea используй как смысловые отбивки, но без декоративности; \term{...} — для терминов, \hl{...} — изредка, только для действительно главного. Никаких вводных фраз вроде «в этой лекции мы рассмотрим».
НИКОГДА не используй \@, \makeatletter, \makeatother и другие внутренние макросы — титул создаётся только \makelecturetitle."""

STYLE_BLOCKS: dict[Style, str] = {
    Style.STRICT: STYLE_STRICT,
    Style.COLORFUL: STYLE_COLORFUL,
    Style.NEON: STYLE_NEON,
    Style.NATURE: STYLE_NATURE,
    Style.MINIMAL: STYLE_MINIMAL,
}


# ---------------------------------------------------------------------------
# Блоки типов документов
# ---------------------------------------------------------------------------

_TITLE_RULES = r"""ТИТУЛЬНЫЙ ЛИСТ (обязательно для любого типа):
- Первая строка тела: \makelecturetitle{Тема} — один аргумент, только тема документа (без слова «тема», без кавычек).
- Тип документа на титуле подставляет приложение (\universumdoctype) — тебе его писать НЕ нужно и запрещено.
- Доп. данные (курс, семестр, модуль, лектор, группа, вуз) можно передать необязательным аргументом:
  \makelecturetitle[Курс: … · Семестр: … · Лектор: …]{Тема}
  ТОЛЬКО если пользователь указал их в запросе или в исходном файле. Если не указал — не добавляй ни одного такого поля:
  никаких «Семестр 2», «Модуль 1», «Лектор: Фамилия И.О.», «Кафедра …», «Группа …», «Лекция № 5», которые ты придумал сам.
- Никаких \title, \maketitle, \author, \date, \begin{titlepage} — они запрещены."""

DOCTYPE_LECTURE = r"""ТИП ДОКУМЕНТА: ЛЕКЦИЯ.
Структура (строго в этом порядке):
1. Титульный лист: \makelecturetitle{Тема} (см. правила титула; тип «Лекция» подставит приложение).
2. \tableofcontents (если документ длинный).
3. Введение (\section{Введение}): актуальность, цели и задачи, что студент будет знать/уметь после.
4. Основные разделы: 3–5 логических разделов (или по структуре пользователя). В каждом — теория, минимум один блок definition/theorem, пример в example.
5. Примеры/кейсы: 1–2 развёрнутых примера с пошаговым разбором в блоке example.
6. Выводы (\section{Выводы}): 4–7 пунктов + финальный keyidea.
7. Литература (\section*{Литература}): 3–6 реальных источников.
Требования: академический тон, определения терминов, формулы с пояснениями."""

DOCTYPE_SUMMARY = r"""ТИП ДОКУМЕНТА: КОНСПЕКТ.
Структура (сжатая, без воды):
1. Титульный лист: \makelecturetitle{Тема} (тип «Конспект» подставит приложение).
2. Краткое вступление (2–4 предложения).
3. \section{Ключевые тезисы}: маркированный список главных идей.
4. \section{Определения}: термины — каждый в блоке definition (или списком, если их много).
5. \section{Формулы}: ключевые формулы с расшифровкой символов.
6. \section{Разбор темы}: 2–4 коротких раздела с примерами в example.
7. \section*{Итог}: keyidea + таблица «самое важное за 1 страницу».
Требования: максимальная плотность, минимум воды, блоки — короткие и по делу."""

DOCTYPE_HOMEWORK = r"""ТИП ДОКУМЕНТА: ДОМАШНЕЕ ЗАДАНИЕ.
Структура:
1. Титульный лист: \makelecturetitle{Тема} (тип «Домашнее задание» подставит приложение).
2. Инструкция: формат сдачи, что оценивается (срок — только если пользователь его указал; не выдумывай даты).
3. Задания по уровням: базовый (2–3), стандартный (2–3), повышенный (1–2). Каждое задание — с чёткой формулировкой, данными, ожидаемым результатом; нумерация обязательна.
4. Требования к оформлению работы (1 абзац).
5. Критерии оценивания — таблица «критерий — баллы».
ВАЖНО: не давай решений и не раскрывай ответы. Определения и теоремы, нужные для решения, оформи в definition/theorem (это не ответы). Где полезно — подсказка в important."""

DOCTYPE_TEST = r"""ТИП ДОКУМЕНТА: ТЕСТ (несколько вариантов).
Структура (строго в этом порядке):
1. Титульный лист: \makelecturetitle{Тема} (тип «Тест» подставит приложение).
2. Инструкции для студентов (без \clearpage): сколько вариантов, сколько вопросов в каждом, как отвечать, сколько времени (время — только если пользователь его указал).
3. ВАРИАНТЫ. Ровно {variants} варианта. Каждый вариант ОБЯЗАТЕЛЬНО начинается с новой страницы:
   \clearpage
   \section*{Вариант 1}      % затем «Вариант 2», «Вариант 3» … (слово «Вариант» — на языке документа)
   Внутри варианта — ровно {questions} вопросов. Нумерация в каждом варианте НАЧИНАЕТСЯ ЗАНОВО с 1: для каждого варианта свой отдельный список enumerate (не продолжай нумерацию из предыдущего варианта, не используй \setcounter).
   Вопросы разных вариантов НЕ должны повторяться: составляй разные задания по одной теме (тот же уровень сложности).
4. КЛЮЧ ОТВЕТОВ — всегда отдельно, всегда с новой страницы и как отдельный раздел:
   \clearpage
   \section*{Ключ ответов}   % на языке документа: «Ключ ответов» / «Answer key»
   Внутри — по одному подразделу на вариант: \subsection*{Вариант 1}, \subsection*{Вариант 2} …; в каждом — таблица «№ вопроса — правильный ответ».
   Ключ НИКОГДА не должен начинаться на той же странице, где вопросы, и не должен быть вперемешку с вопросами; ответы рядом с вопросами писать запрещено.
Компактность: формулировка вопроса — 1–2 строки, варианты ответа — короткие (до 8 слов), без вступлений и повторов инструкций; так весь тест гарантированно помещается в ответ.
Формат вопросов: у каждого вопроса 4 варианта ответа с явными русскими метками — внутри списка пиши \item[а)] …, \item[б)] …, \item[в)] …, \item[г)] … (на английском — \item[a)], \item[b)] …). Один верный ответ — если пользователь не попросил множественный выбор (тогда 4–5 вариантов и явная пометка «выберите все верные»).
Требования: вопросы строго по теме; дистракторы правдоподобные, но неверные; никакой двусмысленности; нумерация вопросов внутри каждого варианта — 1…{questions}."""

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
- Если в логе «You can't use \spacefactor in internal vertical mode», «Undefined control sequence \@» или упоминаются \makeatletter/\makeatother — удали ВСЕ \@-конструкции, а титул собери только через \makelecturetitle{Тема}.
- Титул всегда ровно в таком виде: \makelecturetitle{Тема}. Если в старом варианте было несколько аргументов (\makelecturetitle{Курс}{Семестр}{Лекция}{Тема}) — оставь ТОЛЬКО последний (тему): \makelecturetitle{Тема}. Не добавляй семестр, модуль, лектора и прочие данные, которых нет в запросе пользователя.
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
        variants = ctx.volume.variants
        questions = ctx.volume.questions
        return (
            f"ОБЪЁМ: ровно {variants} вариантов теста (Вариант 1 … Вариант {variants}), и в КАЖДОМ варианте — "
            f"ровно {questions} вопросов. Вопросы внутри варианта нумеруются заново с 1. "
            f"У каждого вопроса 4 варианта ответа (а, б, в, г). "
            f"Ключ ответов — отдельным разделом после всех вариантов, с новой страницы, с ответами на все {questions} "
            f"вопросов каждого из {variants} вариантов."
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
    doc_block = DOCTYPE_BLOCKS[ctx.doc_type]
    if ctx.doc_type == DocType.TEST:
        # Подставляем реальные числа вариантов/вопросов из интерфейса
        doc_block = (
            doc_block.replace("{variants}", str(ctx.volume.variants))
            .replace("{questions}", str(ctx.volume.questions))
        )
    parts: list[str] = [
        SYSTEM_PROMPT,
        "",
        _TITLE_RULES,
        "",
        STYLE_BLOCKS[ctx.style],
        "",
        doc_block,
        "",
        volume_hint(ctx),
        "",
        f"ЯЗЫК ДОКУМЕНТА: {lang_name}. Весь документ (заголовки, текст, подписи) — на этом языке.",
        "",
        r"НАПОМИНАНИЕ: верни ТОЛЬКО тело (без \documentclass, без \usepackage, без \begin{document}/\end{document}). "
        r"Первая строка тела — \makelecturetitle{Тема} с одной темой в фигурных скобках.",
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
        r"Начни ответ сразу с \makelecturetitle{Тема}. Никакого текста перед ним.",
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
