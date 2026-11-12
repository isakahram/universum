"""Графический интерфейс Universum на customtkinter (современный минимализм).

Экран собран сверху вниз:
    Universum (заголовок) + подзаголовок
    большое поле запроса с плейсхолдером
    компактная панель: тип документа · стиль · объём
    большая кнопка «Сгенерировать PDF»
    прогресс и статус
    «Дополнительно» (файл, структура, веб-поиск) — свёрнуто по умолчанию

Технические детали (преамбула, движок LaTeX, пути) в главном окне не показываются:
настройки нейросети спрятаны за кнопкой «⚙», лог компилятора — в диалоге ошибки.

Вся бизнес-логика остаётся в :class:`universum.orchestrator.Orchestrator`:
GUI только собирает :class:`GenerationRequest`, запускает генерацию в фоновом
потоке и показывает прогресс. Уведомления — всплывающие «toast» и аккуратные
модальные окна вместо стандартных messagebox.
"""

from __future__ import annotations

import queue
import shutil
import threading
from pathlib import Path
from tkinter import filedialog
from typing import Callable

try:  # pragma: no cover — зависит от окружения
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover
    raise ImportError(
        "Для интерфейса нужен пакет customtkinter. "
        "Установите зависимости: pip install -r requirements.txt "
        "(или pip install customtkinter)"
    ) from exc

from .config import (
    PROVIDER_PRESETS,
    DocType,
    LlmConfig,
    LlmProvider,
    LlmSettings,
    Style,
    Volume,
    is_localhost_url,
    load_llm_settings,
    mask_key,
    normalize_api_key,
    save_llm_settings,
)
from .llm_client import LlmClient
from .orchestrator import GenerationRequest, Orchestrator
from .utils import LatexCompileError, UniversumError, get_logger, plural_ru

# ---------------------------------------------------------------------------
# Оформление
# ---------------------------------------------------------------------------

# (светлая тема, тёмная тема)
COLOR_BG = ("#f4f6fb", "#14161a")
COLOR_CARD = ("#ffffff", "#1c1f26")
COLOR_TEXT = ("#111827", "#f3f4f6")
COLOR_MUTED = ("#6b7280", "#9ca3af")
COLOR_LINE = ("#e2e6ef", "#2a2e37")  # тонкие рамки карточек и полей
COLOR_ACCENT = "#2563eb"
COLOR_ACCENT_HOVER = "#1d4ed8"
COLOR_SUCCESS = "#16a34a"
COLOR_ERROR = "#dc2626"
COLOR_WARNING = "#d97706"

# ---------------------------------------------------------------------------
# Горячие клавиши: работают и на английской, и на русской раскладке
# ---------------------------------------------------------------------------
# На русской раскладке Tk присылает не «c», а кириллический keysym
# (Cyrillic_es для «с» и т.д.), поэтому стандартные <Control-c> не срабатывают.
# Сопоставляем keysym → латинскую букву и обрабатываем сами.
_KEYSYM_TO_LETTER: dict[str, str] = {
    "Cyrillic_es": "c",      # с
    "Cyrillic_em": "v",      # м
    "Cyrillic_che": "x",     # ч
    "Cyrillic_ef": "a",      # ф
    "Cyrillic_ya": "z",      # я
    "Cyrillic_en": "y",      # н
    "Cyrillic_yeru": "s",    # ы
    "Cyrillic_shcha": "o",   # щ
    "Cyrillic_ka": "r",      # к
    "Cyrillic_tse": "w",     # ц
}


def _hotkey_letter(keysym: str | None) -> str | None:
    """Латинская буква для комбинации Ctrl+<клавиша> в любой раскладке."""
    if not keysym:
        return None
    mapped = _KEYSYM_TO_LETTER.get(keysym)
    if mapped:
        return mapped
    low = keysym.lower()
    if len(low) == 1 and "a" <= low <= "z":
        return low
    return None


def _text_selection(box) -> tuple[str, str] | None:
    """Границы выделения в многострочном поле (или None)."""
    try:
        ranges = box.tag_ranges("sel")
    except Exception:  # pragma: no cover — виджет без тегов
        return None
    if not ranges:
        return None
    return str(ranges[0]), str(ranges[-1])


def _copy_text(box) -> bool:
    sel = _text_selection(box)
    if not sel:
        return False
    text = box.get(*sel)
    if not text:
        return False
    box.clipboard_clear()
    box.clipboard_append(text)
    return True


def _cut_text(box) -> bool:
    sel = _text_selection(box)
    if not sel:
        return False
    text = box.get(*sel)
    if text:
        box.clipboard_clear()
        box.clipboard_append(text)
        try:
            box.delete(*sel)  # в read-only поле (лог LaTeX) может быть запрещено
        except Exception:
            pass
    return True


def _paste_text(box) -> bool:
    try:
        text = box.clipboard_get()
    except Exception:  # пустой или недоступный буфер обмена
        return False
    try:
        sel = _text_selection(box)
        if sel:
            box.delete(*sel)
        box.insert("insert", text)
    except Exception:  # read-only поле — вставка невозможна
        return False
    return True


def _select_all_text(box) -> bool:
    try:
        box.tag_add("sel", "1.0", "end-1c")
    except Exception:  # pragma: no cover
        return False
    return True


def _undo_text(box) -> bool:
    try:
        box.edit_undo()
    except Exception:
        return False
    return True


def _redo_text(box) -> bool:
    try:
        box.edit_redo()
    except Exception:
        return False
    return True


_TEXT_ACTIONS = {
    "c": _copy_text,
    "x": _cut_text,
    "v": _paste_text,
    "a": _select_all_text,
    "z": _undo_text,
    "y": _redo_text,
}


def install_text_hotkeys(box) -> None:
    """Ctrl+C/V/X/A/Z для многострочного поля в любой раскладке.

    Args:
        box: ``CTkTextbox`` (или совместимый виджет с ``tag_ranges``/``insert``).
    """

    def _on_ctrl(event):
        letter = _hotkey_letter(getattr(event, "keysym", None))
        action = _TEXT_ACTIONS.get(letter or "")
        if action is None:
            return None
        action(box)
        return "break"  # не даём классу Tk сделать то же самое второй раз

    box.bind("<Control-KeyPress>", _on_ctrl)


def _entry_selection(box) -> tuple[str, str] | None:
    """Границы выделения в однострочном поле (или None)."""
    try:
        if not box.select_present():
            return None
        return "sel.first", "sel.last"
    except Exception:  # pragma: no cover
        return None


def install_entry_hotkeys(box) -> None:
    """Ctrl+C/V/X/A/Z для однострочного поля (``CTkEntry``) в любой раскладке."""

    def _copy() -> bool:
        sel = _entry_selection(box)
        if not sel:
            return False
        start, end = int(box.index(sel[0])), int(box.index(sel[1]))
        text = box.get()[start:end]
        if not text:
            return False
        box.clipboard_clear()
        box.clipboard_append(text)
        return True

    def _delete_selection() -> None:
        if _entry_selection(box):
            box.delete("sel.first", "sel.last")

    def _cut() -> bool:
        if not _copy():
            return False
        _delete_selection()
        return True

    def _paste() -> bool:
        try:
            text = box.clipboard_get()
        except Exception:
            return False
        _delete_selection()
        box.insert("insert", text)
        return True

    def _select_all() -> bool:
        box.select_range(0, "end")
        box.icursor("end")
        return True

    actions = {
        "c": _copy,
        "x": _cut,
        "v": _paste,
        "a": _select_all,
    }

    def _on_ctrl(event):
        letter = _hotkey_letter(getattr(event, "keysym", None))
        action = actions.get(letter or "")
        if action is None:
            return None
        action()
        return "break"

    box.bind("<Control-KeyPress>", _on_ctrl)


PLACEHOLDER_REQUEST = "Опишите, что вы хотите сгенерировать…"
PLACEHOLDER_STRUCTURE = "Структура документа (необязательно): разделы по порядку"

_TOAST_COLORS = {
    "success": COLOR_SUCCESS,
    "error": COLOR_ERROR,
    "warning": COLOR_WARNING,
    "info": COLOR_ACCENT,
}
_TOAST_ICONS = {"success": "✓", "error": "✕", "warning": "!", "info": "i"}


# ---------------------------------------------------------------------------
# Всплывающие уведомления
# ---------------------------------------------------------------------------

class Toast(ctk.CTkToplevel):
    """Всплывающее уведомление в правом нижнем углу окна.

    Не перехватывает фокус, исчезает само. Несколько уведомлений
    выстраиваются стопкой вверх.
    """

    _active: list["Toast"] = []

    def __init__(self, master, message: str, kind: str = "info", duration: int = 3600) -> None:
        super().__init__(master)
        self.message = message
        self.kind = kind
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
            self.attributes("-alpha", 0.97)
        except Exception:  # pragma: no cover — не все ОС поддерживают alpha
            pass

        width, height = 400, 76
        color = _TOAST_COLORS.get(kind, COLOR_ACCENT)
        card = ctk.CTkFrame(self, fg_color=color, corner_radius=12)
        card.pack(fill="both", expand=True)

        ctk.CTkLabel(
            card,
            text=_TOAST_ICONS.get(kind, "i"),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#ffffff",
            width=28,
        ).pack(side="left", padx=(14, 4), pady=12)

        ctk.CTkLabel(
            card,
            text=self._shorten(message),
            font=ctk.CTkFont(size=13),
            text_color="#ffffff",
            justify="left",
            anchor="w",
            wraplength=330,
        ).pack(side="left", padx=(0, 14), pady=12, fill="both", expand=True)

        self._place(master, width, height, len(Toast._active))
        Toast._active.append(self)
        self.after(duration, self._close)

    @staticmethod
    def _shorten(message: str, limit: int = 180) -> str:
        text = " ".join(str(message).split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _place(self, master, width: int, height: int, slot: int) -> None:
        try:
            master.update_idletasks()
            x = master.winfo_rootx() + max(0, master.winfo_width() - width - 28)
            y = master.winfo_rooty() + max(0, master.winfo_height() - height - 28 - slot * (height + 10))
            self.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:  # pragma: no cover
            self.geometry(f"{width}x{height}")

    def _close(self) -> None:
        """Плавно скрыть и уничтожить уведомление."""
        try:
            for alpha in (0.7, 0.4, 0.15):
                self.attributes("-alpha", alpha)
                self.update_idletasks()
        except Exception:  # pragma: no cover
            pass
        if self in Toast._active:
            Toast._active.remove(self)
        self.destroy()


# ---------------------------------------------------------------------------
# Модальные диалоги
# ---------------------------------------------------------------------------

class MessageDialog(ctk.CTkToplevel):
    """Аккуратное модальное окно: сообщение + необязательные детали.

    Заменяет стандартные ``messagebox`` — с современным видом и возможностью
    скопировать технический текст (например, лог компилятора).
    """

    def __init__(
        self,
        master,
        title: str,
        message: str,
        *,
        kind: str = "info",
        details: str | None = None,
        on_copy: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title(title)
        self.configure(fg_color=COLOR_BG)
        self.resizable(False, bool(details))
        self.transient(master)
        self.message = message
        self.details_text = details or ""
        self._details = self.details_text
        self.details_box: ctk.CTkTextbox | None = None
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=22, pady=20)

        color = _TOAST_COLORS.get(kind, COLOR_ACCENT)
        header = ctk.CTkFrame(wrap, fg_color="transparent")
        header.pack(fill="x")
        ctk.CTkLabel(
            header,
            text=_TOAST_ICONS.get(kind, "i"),
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=color,
            width=26,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            header,
            text=title,
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=COLOR_TEXT,
            anchor="w",
            justify="left",
        ).pack(side="left")

        ctk.CTkLabel(
            wrap,
            text=message,
            font=ctk.CTkFont(size=13),
            text_color=COLOR_TEXT,
            justify="left",
            anchor="w",
            wraplength=520,
        ).pack(fill="x", pady=(12, 0))

        if details:
            self.geometry("720x520")
            box = ctk.CTkTextbox(
                wrap,
                height=300,
                corner_radius=10,
                font=ctk.CTkFont(family="Consolas", size=11),
                fg_color=COLOR_CARD,
                text_color=COLOR_TEXT,
                wrap="none",
            )
            box.pack(fill="both", expand=True, pady=(14, 0))
            box.insert("1.0", details)
            box.configure(state="disabled")
            install_text_hotkeys(box)
            self.details_box = box
        else:
            self.geometry("480x240")

        buttons = ctk.CTkFrame(wrap, fg_color="transparent")
        buttons.pack(fill="x", pady=(16, 0))

        if details:
            ctk.CTkButton(
                buttons,
                text="Копировать",
                width=130,
                height=34,
                corner_radius=9,
                fg_color="transparent",
                border_width=1,
                border_color=COLOR_LINE,
                text_color=COLOR_TEXT,
                hover_color=COLOR_CARD,
                command=lambda: self._copy(on_copy),
            ).pack(side="left")

        ctk.CTkButton(
            buttons,
            text="Закрыть",
            width=130,
            height=34,
            corner_radius=9,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            command=self.destroy,
        ).pack(side="right")

        self.after(60, self._make_modal)

    def _make_modal(self) -> None:
        """Показать окно по центру родителя и взять фокус."""
        try:
            self.update_idletasks()
            parent = self.master
            x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 3)
            self.geometry(f"+{x}+{y}")
            self.lift()
            self.grab_set()
            self.focus_force()
        except Exception:  # pragma: no cover
            pass

    def _copy(self, on_copy: Callable[[], None] | None) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(self._details)
            self.update_idletasks()
        except Exception:  # pragma: no cover
            pass
        if on_copy is not None:
            on_copy()


# ---------------------------------------------------------------------------
# Поле с плейсхолдером
# ---------------------------------------------------------------------------

class PlaceholderTextbox(ctk.CTkTextbox):
    """Многострочное поле, показывающее подсказку, пока оно пустое."""

    def __init__(self, master, placeholder: str, **kwargs) -> None:
        super().__init__(master, **kwargs)
        self._placeholder = placeholder
        self._showing_placeholder = False
        self.bind("<FocusIn>", self._on_focus_in, add="+")
        self.bind("<FocusOut>", self._on_focus_out, add="+")
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        if self._showing_placeholder:
            return
        if self.get("1.0", "end-1c").strip() == "":
            self._showing_placeholder = True
            self.insert("1.0", self._placeholder)
            self.configure(text_color=COLOR_MUTED)

    def _hide_placeholder(self) -> None:
        if self._showing_placeholder:
            self._showing_placeholder = False
            self.delete("1.0", "end")
            self.configure(text_color=COLOR_TEXT)

    def _on_focus_in(self, _event=None) -> None:
        self._hide_placeholder()

    def _on_focus_out(self, _event=None) -> None:
        self._show_placeholder()

    def get_value(self) -> str:
        """Текст без плейсхолдера."""
        return "" if self._showing_placeholder else self.get("1.0", "end-1c").strip()

    def set_value(self, text: str) -> None:
        """Записать текст (плейсхолдер скрывается)."""
        self._hide_placeholder()
        self.delete("1.0", "end")
        if text:
            self.insert("1.0", text)
        else:
            self._show_placeholder()


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------

class UniversumApp(ctk.CTk):
    """Главное окно приложения."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Universum")
        self.geometry("980x860")
        self.minsize(880, 740)
        self.configure(fg_color=COLOR_BG)

        self.logger = get_logger()
        self.orchestrator = Orchestrator()
        self._progress_queue: queue.Queue[str] = queue.Queue()
        self._ui_calls: queue.Queue[Callable[[], None]] = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._result_pdf: Path | None = None
        self._file_path: str = ""
        self._extra_visible = False
        self._dialogs: list[ctk.CTkToplevel] = []
        self._settings_win: ctk.CTkToplevel | None = None
        # создаются позже (только для тестов / в блоке «Дополнительно»)
        self.variants_slider: ctk.CTkSlider | None = None
        self.structure_box: PlaceholderTextbox | None = None
        self.search_switch: ctk.CTkSwitch | None = None

        self._build_ui()
        self._bind_shortcuts()
        self.after(120, self._poll_queue)

    # -- горячие клавиши -------------------------------------------------

    def _bind_shortcuts(self) -> None:
        """Ctrl+Enter / Ctrl+S / Ctrl+O — в любой раскладке.

        Enter, S и O на русской раскладке приходят как кириллические keysym
        (``Cyrillic_yeru``, ``Cyrillic_shcha``), поэтому обрабатываем буквы сами.
        """
        for sequence in ("<Control-Return>", "<Control-KP_Enter>"):
            self.bind(sequence, self._on_generate_shortcut)

        def _on_ctrl(event):
            letter = _hotkey_letter(getattr(event, "keysym", None))
            if letter == "s":
                self._on_save()
                return "break"
            if letter == "o":
                self._choose_file()
                return "break"
            return None

        self.bind("<Control-KeyPress>", _on_ctrl)

    def _on_generate_shortcut(self, _event=None):
        """Ctrl+Enter: запустить генерацию и не вставлять перевод строки."""
        self._on_generate()
        return "break"

    # -- построение интерфейса -------------------------------------------

    def _build_ui(self) -> None:
        """Собрать экран сверху вниз."""
        page = ctk.CTkFrame(self, fg_color="transparent")
        page.pack(fill="both", expand=True, padx=30, pady=(24, 18))

        # --- заголовок ---
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.pack(fill="x")

        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", anchor="w")
        ctk.CTkLabel(
            titles,
            text="Universum",
            font=ctk.CTkFont(size=34, weight="bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="Учебные материалы в PDF за пару минут",
            font=ctk.CTkFont(size=14),
            text_color=COLOR_MUTED,
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        self.settings_btn = ctk.CTkButton(
            header,
            text="⚙",
            width=40,
            height=40,
            corner_radius=20,
            font=ctk.CTkFont(size=17),
            fg_color="transparent",
            border_width=1,
            border_color=COLOR_LINE,
            text_color=COLOR_MUTED,
            hover_color=COLOR_CARD,
            command=self._open_settings,
        )
        self.settings_btn.pack(side="right", pady=(6, 0))

        # --- запрос ---
        ctk.CTkLabel(
            page,
            text="Что сгенерировать?",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=COLOR_MUTED,
            anchor="w",
        ).pack(fill="x", pady=(24, 6))

        self.request_box = PlaceholderTextbox(
            page,
            PLACEHOLDER_REQUEST,
            height=150,
            corner_radius=14,
            border_width=1,
            border_color=COLOR_LINE,
            fg_color=COLOR_CARD,
            text_color=COLOR_TEXT,
            font=ctk.CTkFont(size=15),
            wrap="word",
        )
        self.request_box.pack(fill="x")
        install_text_hotkeys(self.request_box)

        # --- панель настроек в один ряд ---
        panel = ctk.CTkFrame(page, fg_color="transparent")
        panel.pack(fill="x", pady=(20, 0))

        self.doc_menu = self._labeled_option(
            panel,
            "Тип документа",
            [dt.label_ru for dt in DocType],
            on_change=self._on_type_changed,
            width=200,
        )
        self.style_menu = self._labeled_option(
            panel,
            "Стиль",
            [st.label_ru for st in Style],
            on_change=self._on_style_changed,
            width=200,
        )

        volume = ctk.CTkFrame(panel, fg_color="transparent")
        volume.pack(side="right", anchor="n")
        self.volume_title = ctk.CTkLabel(
            volume,
            text="Объём",
            font=ctk.CTkFont(size=12),
            text_color=COLOR_MUTED,
            anchor="w",
        )
        self.volume_title.pack(anchor="w")
        self.volume_slider = ctk.CTkSlider(
            volume,
            from_=1,
            to=30,
            number_of_steps=29,
            width=220,
            progress_color=COLOR_ACCENT,
            button_color=COLOR_ACCENT,
            button_hover_color=COLOR_ACCENT_HOVER,
            command=self._on_volume_changed,
        )
        self.volume_slider.set(5)
        self.volume_slider.pack(anchor="w", pady=(10, 0))
        self.volume_value = ctk.CTkLabel(
            volume,
            text="5 страниц",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        )
        self.volume_value.pack(anchor="w", pady=(4, 0))

        self.variants_row = ctk.CTkFrame(panel, fg_color="transparent")

        # --- основная кнопка ---
        self.generate_btn = ctk.CTkButton(
            page,
            text="Сгенерировать PDF",
            height=54,
            corner_radius=14,
            font=ctk.CTkFont(size=17, weight="bold"),
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            command=self._on_generate,
        )
        self.generate_btn.pack(fill="x", pady=(22, 0))

        # --- прогресс и статус ---
        # контейнер постоянной высоты: полоса появляется только во время работы
        self.progress_slot = ctk.CTkFrame(page, fg_color="transparent", height=22)
        self.progress_slot.pack(fill="x", pady=(16, 0))
        self.progress = ctk.CTkProgressBar(
            self.progress_slot,
            mode="indeterminate",
            height=6,
            corner_radius=3,
            progress_color=COLOR_ACCENT,
            fg_color=COLOR_CARD,
        )
        self.progress.set(0)

        status_row = ctk.CTkFrame(page, fg_color="transparent")
        status_row.pack(fill="x", pady=(8, 0))

        self.status_label = ctk.CTkLabel(
            status_row,
            text="Введите запрос и нажмите «Сгенерировать PDF» (Ctrl+Enter)",
            font=ctk.CTkFont(size=13),
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=640,
        )
        self.status_label.pack(side="left", anchor="w")

        self.cancel_btn = ctk.CTkButton(
            status_row,
            text="Отмена",
            width=110,
            height=34,
            corner_radius=9,
            fg_color="transparent",
            border_width=1,
            border_color=COLOR_LINE,
            text_color=COLOR_TEXT,
            hover_color=COLOR_CARD,
            state="disabled",
            command=self._on_cancel,
        )
        self.cancel_btn.pack(side="right")

        self.save_btn = ctk.CTkButton(
            status_row,
            text="Сохранить PDF",
            width=150,
            height=34,
            corner_radius=9,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=COLOR_SUCCESS,
            hover_color="#15803d",
            state="disabled",
            command=self._on_save,
        )
        self.save_btn.pack(side="right", padx=(0, 8))

        # --- «Дополнительно» ---
        self.extra_toggle = ctk.CTkButton(
            page,
            text="Дополнительно ▾",
            height=34,
            corner_radius=9,
            fg_color="transparent",
            text_color=COLOR_MUTED,
            hover_color=COLOR_CARD,
            font=ctk.CTkFont(size=12),
            command=self._toggle_extra,
        )
        self.extra_toggle.pack(anchor="w", pady=(18, 0))

        self.extra_frame = ctk.CTkFrame(page, fg_color=COLOR_CARD, corner_radius=14)
        self._build_extra(self.extra_frame)

    def _labeled_option(
        self,
        master,
        title: str,
        values: list[str],
        *,
        on_change: Callable[[str], None],
        width: int = 200,
    ) -> ctk.CTkOptionMenu:
        """Подпись + выпадающий список в общей панели."""
        holder = ctk.CTkFrame(master, fg_color="transparent")
        holder.pack(side="left", padx=(0, 18), anchor="n")
        ctk.CTkLabel(
            holder,
            text=title,
            font=ctk.CTkFont(size=12),
            text_color=COLOR_MUTED,
            anchor="w",
        ).pack(anchor="w")
        menu = ctk.CTkOptionMenu(
            holder,
            values=values,
            width=width,
            height=36,
            corner_radius=9,
            font=ctk.CTkFont(size=13),
            fg_color=COLOR_CARD,
            button_color=COLOR_CARD,
            button_hover_color=COLOR_BG,
            text_color=COLOR_TEXT,
            dropdown_fg_color=COLOR_CARD,
            dropdown_text_color=COLOR_TEXT,
            command=on_change,
        )
        menu.set(values[0])
        menu.pack(anchor="w", pady=(8, 0))
        return menu

    def _build_extra(self, parent) -> None:
        """Скрытая по умолчанию часть: файл, структура, веб-поиск."""
        inner = ctk.CTkFrame(parent, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=14)

        file_row = ctk.CTkFrame(inner, fg_color="transparent")
        file_row.pack(fill="x")
        ctk.CTkLabel(
            file_row,
            text="Материал",
            font=ctk.CTkFont(size=12),
            text_color=COLOR_MUTED,
            width=90,
            anchor="w",
        ).pack(side="left")
        self.file_btn = ctk.CTkButton(
            file_row,
            text="Прикрепить файл",
            width=150,
            height=32,
            corner_radius=8,
            fg_color="transparent",
            border_width=1,
            border_color=COLOR_LINE,
            text_color=COLOR_TEXT,
            hover_color=COLOR_BG,
            command=self._choose_file,
        )
        self.file_btn.pack(side="left")
        self.file_label = ctk.CTkLabel(
            file_row,
            text="не выбран",
            font=ctk.CTkFont(size=12),
            text_color=COLOR_MUTED,
            anchor="w",
        )
        self.file_label.pack(side="left", padx=(10, 0))
        self.file_reset_btn = ctk.CTkButton(
            file_row,
            text="Сбросить",
            width=90,
            height=32,
            corner_radius=8,
            fg_color="transparent",
            text_color=COLOR_MUTED,
            hover_color=COLOR_BG,
            command=self._reset_file,
        )
        self.file_reset_btn.pack(side="right")

        self.structure_box = PlaceholderTextbox(
            inner,
            PLACEHOLDER_STRUCTURE,
            height=84,
            corner_radius=10,
            border_width=1,
            border_color=COLOR_LINE,
            fg_color=COLOR_BG,
            text_color=COLOR_TEXT,
            font=ctk.CTkFont(size=13),
            wrap="word",
        )
        self.structure_box.pack(fill="x", pady=(12, 0))
        install_text_hotkeys(self.structure_box)

        self.search_switch = ctk.CTkSwitch(
            inner,
            text="Искать свежие данные в интернете",
            font=ctk.CTkFont(size=13),
            text_color=COLOR_TEXT,
            progress_color=COLOR_ACCENT,
        )
        self.search_switch.pack(anchor="w", pady=(12, 0))

    def _toggle_extra(self) -> None:
        """Показать/скрыть блок «Дополнительно»."""
        self._extra_visible = not self._extra_visible
        if self._extra_visible:
            self.extra_frame.pack(fill="x", pady=(10, 0))
            self.extra_toggle.configure(text="Дополнительно ▴")
        else:
            self.extra_frame.pack_forget()
            self.extra_toggle.configure(text="Дополнительно ▾")

    # -- параметры --------------------------------------------------------

    def _current_doc_type(self) -> DocType:
        label = self.doc_menu.get()
        for dt in DocType:
            if dt.label_ru == label:
                return dt
        return DocType.LECTURE

    def _current_style(self) -> Style:
        label = self.style_menu.get()
        for st in Style:
            if st.label_ru == label:
                return st
        return Style.STRICT

    def _on_type_changed(self, _value: str | None = None) -> None:
        """У теста объём — вопросы, у остальных — страницы."""
        if self._current_doc_type() == DocType.TEST:
            self.volume_title.configure(text="Вопросов в каждом варианте")
            self.volume_slider.configure(from_=5, to=50, number_of_steps=45)
            self.volume_slider.set(10)
            self.variants_row.pack(side="right", anchor="n", padx=(0, 20))
            if self.variants_slider is None:
                ctk.CTkLabel(
                    self.variants_row,
                    text="Вариантов теста",
                    font=ctk.CTkFont(size=12),
                    text_color=COLOR_MUTED,
                    anchor="w",
                ).pack(anchor="w")
                self.variants_slider = ctk.CTkSlider(
                    self.variants_row,
                    from_=2,
                    to=6,
                    number_of_steps=4,
                    width=160,
                    progress_color=COLOR_ACCENT,
                    button_color=COLOR_ACCENT,
                    button_hover_color=COLOR_ACCENT_HOVER,
                    command=self._on_volume_changed,
                )
                self.variants_slider.set(2)
                self.variants_slider.pack(anchor="w", pady=(10, 0))
        else:
            self.volume_title.configure(text="Объём (страниц)")
            self.volume_slider.configure(from_=1, to=30, number_of_steps=29)
            self.volume_slider.set(5)
            self.variants_row.pack_forget()
        self._on_volume_changed()

    def _on_style_changed(self, _value: str | None = None) -> None:
        """Смена стиля сразу видна в статусе — без технических подробностей."""
        style = self._current_style()
        self.status_label.configure(text=f"Стиль: {style.label_ru.lower()}", text_color=COLOR_MUTED)

    def _on_volume_changed(self, _value: float | None = None) -> None:
        if self._current_doc_type() == DocType.TEST:
            questions = int(round(self.volume_slider.get()))
            variants = int(round(self.variants_slider.get())) if self.variants_slider else 2
            self.volume_value.configure(
                text=f"{questions} {_plural_questions(questions)} в варианте · "
                f"{variants} {_plural_variants(variants)}"
            )
        else:
            pages = int(round(self.volume_slider.get()))
            self.volume_value.configure(text=f"{pages} {_plural_pages(pages)}")

    # -- файл -------------------------------------------------------------

    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите файл",
            filetypes=[
                ("Все поддерживаемые", "*.pdf *.docx *.doc *.txt *.pptx *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp"),
                ("Все файлы", "*.*"),
            ],
        )
        if path:
            self._file_path = path
            self.file_label.configure(text=Path(path).name, text_color=COLOR_TEXT)

    def _reset_file(self) -> None:
        self._file_path = ""
        self.file_label.configure(text="не выбран", text_color=COLOR_MUTED)

    # -- генерация --------------------------------------------------------

    def _collect_request(self) -> GenerationRequest:
        """Собрать запрос для оркестратора (логика не менялась)."""
        dt = self._current_doc_type()
        st = self._current_style()
        pages = int(round(self.volume_slider.get())) if dt != DocType.TEST else 5
        questions = (
            int(round(self.volume_slider.get())) if dt == DocType.TEST else 10
        )
        variants = int(round(self.variants_slider.get())) if self.variants_slider else 4
        volume = Volume(
            pages=max(1, min(30, pages)),
            questions=max(1, min(50, questions)),
            variants=max(2, min(6, variants)),
        )
        structure = self.structure_box.get_value() if self.structure_box else ""
        use_search = bool(self.search_switch.get()) if self.search_switch else False
        return GenerationRequest(
            request_text=self.request_box.get_value(),
            structure_text=structure,
            file_path=self._file_path,
            doc_type=dt,
            style=st,
            volume=volume,
            use_web_search=use_search,
        )

    def _on_generate(self) -> None:
        """Запустить генерацию в фоновом потоке."""
        if self._worker is not None and self._worker.is_alive():
            return
        try:
            self.orchestrator.reload_llm_config()
        except Exception:  # pragma: no cover — настройки могли не сохраниться
            pass

        req = self._collect_request()
        if not req.request_text and not req.file_path:
            self._toast("Введите запрос или прикрепите файл", "warning")
            self.request_box.focus_set()
            return

        self._cancel_event = threading.Event()
        self._result_pdf = None
        self.generate_btn.configure(state="disabled", text="Генерация…")
        self.cancel_btn.configure(state="normal")
        self.save_btn.configure(state="disabled")
        self.progress.pack(fill="x")
        self.progress.start()
        self._set_status("Готовлю материал…", COLOR_TEXT)

        def _worker() -> None:
            try:
                result = self.orchestrator.generate(
                    req,
                    progress=lambda m: self._progress_queue.put(m),
                    cancel_event=self._cancel_event,
                )
                self._progress_queue.put(f"__DONE__{result.pdf_path}")
            except LatexCompileError as e:
                self._progress_queue.put(f"__LATEX_ERROR__{e.log or '(лог компилятора пуст)'}")
            except UniversumError as e:
                self._progress_queue.put(f"__ERROR__{e}")
            except Exception as e:  # pragma: no cover — защита от падений
                self.logger.exception("Unexpected error: %s", e)
                self._progress_queue.put(f"__ERROR__Произошла неожиданная ошибка: {e}")

        self._worker = threading.Thread(target=_worker, daemon=True)
        self._worker.start()

    def _on_cancel(self) -> None:
        self._cancel_event.set()
        self._set_status("Отмена…", COLOR_MUTED)

    def _poll_queue(self) -> None:
        """Забрать сообщения фонового потока (только главный поток)."""
        try:
            while True:
                msg = self._progress_queue.get_nowait()
                if msg.startswith("__DONE__"):
                    self._finish_progress()
                    self._result_pdf = Path(msg[len("__DONE__"):])
                    self.save_btn.configure(state="normal")
                    self._set_status("Готово — PDF можно сохранить", COLOR_SUCCESS)
                    self._toast("PDF готов. Нажмите «Сохранить PDF»", "success")
                elif msg.startswith("__LATEX_ERROR__"):
                    self._finish_progress()
                    log_text = msg[len("__LATEX_ERROR__"):]
                    self._set_status("Не удалось собрать PDF", COLOR_ERROR)
                    self._show_dialog(
                        "Не удалось собрать PDF",
                        "LaTeX не смог скомпилировать документ. Технический лог — ниже.",
                        kind="error",
                        details=log_text,
                    )
                elif msg.startswith("__ERROR__"):
                    self._finish_progress()
                    text = msg[len("__ERROR__"):]
                    self._set_status("Ошибка генерации", COLOR_ERROR)
                    self._show_dialog("Ошибка генерации", text, kind="error")
                else:
                    self._set_status(msg, COLOR_TEXT)
        except queue.Empty:
            pass
        # отложенные вызовы из фоновых потоков выполняем только в главном
        try:
            while True:
                self._ui_calls.get_nowait()()
        except queue.Empty:
            pass
        except Exception as e:  # pragma: no cover — колбэк не должен ломать цикл
            self.logger.exception("UI callback failed: %s", e)
        self.after(120, self._poll_queue)

    def _finish_progress(self) -> None:
        self.progress.stop()
        self.progress.set(0)
        self.progress.pack_forget()
        self.generate_btn.configure(state="normal", text="Сгенерировать PDF")
        self.cancel_btn.configure(state="disabled")

    def _set_status(self, text: str, color=COLOR_TEXT) -> None:
        self.status_label.configure(text=text, text_color=color)

    # -- уведомления ------------------------------------------------------

    def _toast(self, message: str, kind: str = "info") -> None:
        Toast(self, message, kind=kind)

    def _show_dialog(
        self,
        title: str,
        message: str,
        *,
        kind: str = "info",
        details: str | None = None,
    ) -> None:
        dialog = MessageDialog(self, title, message, kind=kind, details=details)
        self._dialogs = [d for d in self._dialogs if d.winfo_exists()]
        self._dialogs.append(dialog)

    # -- сохранение PDF ---------------------------------------------------

    def _on_save(self) -> None:
        if self._result_pdf is None or not self._result_pdf.exists():
            self._toast("PDF ещё не готов", "warning")
            return
        dest = filedialog.asksaveasfilename(
            title="Сохранить PDF",
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
            # имя уже собрано как «Тип — Тема» в оркестраторе
            initialfile=self._result_pdf.name,
        )
        if not dest:
            return
        try:
            shutil.copyfile(self._result_pdf, dest)
            self._toast(f"Сохранено: {Path(dest).name}", "success")
        except Exception as e:  # pragma: no cover — ошибки ФС
            self._show_dialog("Не удалось сохранить файл", str(e), kind="error")

    # -- настройки нейросети ---------------------------------------------

    def _open_settings(self) -> None:
        """Отдельное окно: провайдер, ключ, проверка соединения."""
        if self._settings_win is not None:
            try:
                if self._settings_win.winfo_exists():
                    self._settings_win.lift()
                    self._settings_win.focus_force()
                    return
            except Exception:  # pragma: no cover — окно уже уничтожено
                pass
            self._settings_win = None

        win = ctk.CTkToplevel(self)
        win.title("Подключение")
        win.geometry("620x560")
        win.configure(fg_color=COLOR_BG)
        win.transient(self)

        def _grab() -> None:
            """Модальность включаем после отрисовки, иначе Tcl ругается."""
            try:
                win.grab_set()
                win.focus_force()
            except Exception:  # pragma: no cover
                pass

        win.after(120, _grab)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._settings_win = win
        self._dialogs.append(win)

        settings = load_llm_settings()
        state: dict[str, LlmProvider] = {"provider": settings.provider}

        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=20)

        ctk.CTkLabel(
            body,
            text="Подключение к нейросети",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLOR_TEXT,
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            body,
            text="По умолчанию всё работает бесплатно. Ключ нужен только если сервис его требует.",
            font=ctk.CTkFont(size=12),
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=520,
        ).pack(fill="x", pady=(4, 16))

        provider_labels = [p.label_ru for p in LlmProvider]
        provider_menu = ctk.CTkOptionMenu(
            body,
            values=provider_labels,
            height=36,
            corner_radius=9,
            fg_color=COLOR_CARD,
            button_color=COLOR_CARD,
            button_hover_color=COLOR_BG,
            text_color=COLOR_TEXT,
            dropdown_fg_color=COLOR_CARD,
            dropdown_text_color=COLOR_TEXT,
            command=lambda _v: _on_provider_changed(),
        )
        provider_menu.set(settings.provider.label_ru)
        provider_menu.pack(fill="x", pady=(0, 10))

        url_entry = ctk.CTkEntry(
            body,
            placeholder_text="Адрес сервиса (необязательно)",
            height=36,
            corner_radius=9,
            fg_color=COLOR_CARD,
            text_color=COLOR_TEXT,
        )
        url_entry.insert(0, settings.url)
        url_entry.pack(fill="x", pady=(0, 10))
        install_entry_hotkeys(url_entry)

        model_entry = ctk.CTkEntry(
            body,
            placeholder_text="Модель (необязательно)",
            height=36,
            corner_radius=9,
            fg_color=COLOR_CARD,
            text_color=COLOR_TEXT,
        )
        model_entry.insert(0, settings.model)
        model_entry.pack(fill="x", pady=(0, 10))
        install_entry_hotkeys(model_entry)

        key_entry = ctk.CTkEntry(
            body,
            placeholder_text="Ключ (если сервис его требует)",
            height=36,
            corner_radius=9,
            fg_color=COLOR_CARD,
            text_color=COLOR_TEXT,
            show="•",
        )
        key_entry.insert(0, settings.api_key)
        key_entry.pack(fill="x", pady=(0, 10))
        install_entry_hotkeys(key_entry)

        show_key = ctk.CTkSwitch(body, text="Показать ключ", font=ctk.CTkFont(size=12), text_color=COLOR_MUTED)
        show_key.pack(anchor="w")

        def _toggle_key_visibility() -> None:
            key_entry.configure(show="" if show_key.get() else "•")

        show_key.configure(command=_toggle_key_visibility)

        hint = ctk.CTkLabel(
            body,
            text=f"Текущий ключ: {mask_key(settings.api_key)}",
            font=ctk.CTkFont(size=12),
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
            wraplength=520,
        )
        hint.pack(fill="x", pady=(12, 0))

        def _selected_provider() -> LlmProvider:
            for p in LlmProvider:
                if p.label_ru == provider_menu.get():
                    return p
            return LlmProvider.POLLINATIONS

        def _key_state_text() -> str:
            key = normalize_api_key(key_entry.get())
            if not key:
                return "Ключ не задан — если сервис его требует, запрос вернёт 401."
            url = url_entry.get().strip() or str(PROVIDER_PRESETS.get(_selected_provider(), {}).get("url", ""))
            if is_localhost_url(url):
                return f"Ключ {mask_key(key)} задан, но для локального сервиса не отправляется."
            return f"Ключ {mask_key(key)} будет отправлен в заголовке Authorization."

        def _refresh_hint(*_args) -> None:
            hint.configure(text=_key_state_text())

        def _on_provider_changed() -> None:
            provider = _selected_provider()
            preset = PROVIDER_PRESETS.get(provider, {})
            previous = PROVIDER_PRESETS.get(state["provider"], {})
            current_url = url_entry.get().strip()
            current_model = model_entry.get().strip()
            # не затираем то, что пользователь ввёл сам
            if not current_url or current_url == str(previous.get("url", "")):
                url_entry.delete(0, "end")
                url_entry.insert(0, str(preset.get("url", "")))
            if not current_model or current_model == str(previous.get("model", "")):
                model_entry.delete(0, "end")
                model_entry.insert(0, str(preset.get("model", "")))
            state["provider"] = provider
            _refresh_hint()

        key_entry.bind("<KeyRelease>", _refresh_hint)
        url_entry.bind("<KeyRelease>", _refresh_hint)
        _refresh_hint()

        def _collect() -> LlmSettings:
            return LlmSettings(
                provider=_selected_provider(),
                url=url_entry.get().strip(),
                model=model_entry.get().strip(),
                api_key=key_entry.get().strip(),
            )

        def _on_save() -> None:
            try:
                save_llm_settings(_collect())
                self.orchestrator.reload_llm_config()
                _refresh_hint()
                self._toast("Настройки сохранены", "success")
            except Exception as e:  # pragma: no cover — ошибки ФС/keyring
                self._show_dialog("Не удалось сохранить настройки", str(e), kind="error")

        def _on_test() -> None:
            collected = _collect()
            cfg = LlmConfig.from_settings(collected)
            if normalize_api_key(collected.api_key) and not cfg.api_key:
                hint.configure(text="Ключ не попал в настройки — нажмите «Сохранить».")
                return
            hint.configure(text="Проверяю соединение…")

            def _worker() -> None:
                ok, msg = LlmClient(cfg).test_connection()

                def _apply() -> None:
                    hint.configure(text=msg)
                    self._toast("Подключение работает" if ok else "Не удалось подключиться",
                                "success" if ok else "error")
                    if not ok:
                        self._show_dialog("Проблема с подключением", msg, kind="error")

                self._ui_calls.put(_apply)

            threading.Thread(target=_worker, daemon=True).start()

        def _on_clear_key() -> None:
            key_entry.delete(0, "end")
            _refresh_hint()

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(18, 0))
        ctk.CTkButton(
            buttons,
            text="Проверить",
            width=120,
            height=36,
            corner_radius=9,
            fg_color="transparent",
            border_width=1,
            border_color=COLOR_LINE,
            text_color=COLOR_TEXT,
            hover_color=COLOR_CARD,
            command=_on_test,
        ).pack(side="left")
        ctk.CTkButton(
            buttons,
            text="Очистить ключ",
            width=130,
            height=36,
            corner_radius=9,
            fg_color="transparent",
            text_color=COLOR_MUTED,
            hover_color=COLOR_CARD,
            command=_on_clear_key,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            buttons,
            text="Сохранить",
            width=130,
            height=36,
            corner_radius=9,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            command=_on_save,
        ).pack(side="right")
        ctk.CTkButton(
            buttons,
            text="Закрыть",
            width=110,
            height=36,
            corner_radius=9,
            fg_color="transparent",
            border_width=1,
            border_color=COLOR_LINE,
            text_color=COLOR_TEXT,
            hover_color=COLOR_CARD,
            command=win.destroy,
        ).pack(side="right", padx=(0, 8))


def _plural_questions(n: int) -> str:
    """Склонение слова «вопрос»."""
    return plural_ru(n, "вопрос", "вопроса", "вопросов")


def _plural_variants(n: int) -> str:
    """Склонение слова «вариант» (тест)."""
    return plural_ru(n, "вариант", "варианта", "вариантов")


def _plural_pages(n: int) -> str:
    """Склонение слова «страница» для подписи слайдера."""
    return plural_ru(n, "страница", "страницы", "страниц")


def run() -> None:
    """Запустить приложение."""
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    app = UniversumApp()
    app.mainloop()


if __name__ == "__main__":  # pragma: no cover
    run()
