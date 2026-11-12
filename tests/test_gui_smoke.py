"""Smoke-тесты GUI на заглушках: настоящий Tk в CI недоступен.

Проверяем то, что не видно при обычном `py_compile`: порядок создания виджетов,
сборку :class:`GenerationRequest`, обмен сообщениями с оркестратором и диалоги.
Заглушки строгие: неизвестный метод виджета — ошибка, а не «тихий» no-op.
"""

from __future__ import annotations

import contextlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _Widget:
    """Минимальный «виджет»: запоминает аргументы, привязки и буфер обмена."""

    def __init__(self, master=None, **kwargs):
        self._kwargs = dict(kwargs)
        self._children: list[_Widget] = []
        self._value: object = ""
        self._visible = False
        self._alive = True
        self._command = kwargs.get("command")
        self._bindings: dict[str, object] = {}
        self._clipboard = ""
        self._sel: tuple[str, str] | None = None
        if hasattr(master, "_children"):
            master._children.append(self)

    # -- геометрия ---------------------------------------------------------
    def pack(self, **kwargs):
        self._visible = True

    def grid(self, **kwargs):
        self._visible = True

    def place(self, **kwargs):
        self._visible = True

    def pack_forget(self):
        self._visible = False

    def grid_forget(self):
        self._visible = False

    def grid_columnconfigure(self, *args, **kwargs):
        return None

    def grid_rowconfigure(self, *args, **kwargs):
        return None

    # -- свойства ----------------------------------------------------------
    def configure(self, require_redraw: bool = False, **kwargs):
        self._kwargs.update(kwargs)

    config = configure

    def cget(self, key: str):
        return self._kwargs.get(key)

    def bind(self, sequence=None, command=None, add=True):
        self._bindings[sequence] = command
        return "binding"

    def bind_all(self, sequence=None, command=None, add=True):
        self._bindings[f"all:{sequence}"] = command
        return "binding"

    def focus_get(self):
        return None

    def unbind(self, *args, **kwargs):
        return None

    def set(self, value):
        self._value = value

    def get(self, *args):
        return self._value

    def insert(self, index, text):
        self._value = f"{self._value}{text}"

    def delete(self, *args):
        self._value = ""

    # -- выделение и буфер обмена (горячие клавиши) ------------------------
    def tag_ranges(self, tag):
        if tag == "sel" and self._sel:
            return self._sel
        return ()

    def tag_add(self, tag, start, end=None):
        if tag == "sel":
            self._sel = (start, end)

    def tag_remove(self, *args):
        self._sel = None

    def clipboard_clear(self):
        self._clipboard = ""

    def clipboard_append(self, text):
        self._clipboard += text

    def clipboard_get(self):
        if not self._clipboard:
            raise RuntimeError("буфер обмена пуст")
        return self._clipboard

    def select_present(self):
        return self._sel is not None

    def select_range(self, start, end):
        self._sel = (str(start), str(end))

    def icursor(self, index):
        return len(str(self._value))

    def index(self, index):
        if index == "sel.first":
            return 0
        if index == "sel.last":
            return len(str(self._value))
        return 0

    def edit_undo(self):
        self._value = ""

    def edit_redo(self):
        return None

    def mark_set(self, *args):
        return None

    # -- события/жизненный цикл -------------------------------------------
    def after(self, delay, callback=None, *args):
        return "after#1"

    def update_idletasks(self):
        return None

    def update(self):
        return None

    def destroy(self):
        self._alive = False

    def mainloop(self, *args):
        return None

    def wait_window(self, *args):
        return None

    def winfo_exists(self):
        return 1 if self._alive else 0

    def winfo_children(self):
        return list(self._children)

    def winfo_width(self):
        return 320

    def winfo_height(self):
        return 90

    def winfo_x(self):
        return 10

    def winfo_y(self):
        return 10

    def winfo_rootx(self):
        return 100

    def winfo_rooty(self):
        return 100

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def title(self, *args):
        return None

    def geometry(self, *args):
        return None

    def minsize(self, *args):
        return None

    def maxsize(self, *args):
        return None

    def resizable(self, *args):
        return None

    def transient(self, *args):
        return None

    def protocol(self, *args):
        return None

    def grab_set(self):
        return None

    def grab_release(self):
        return None

    def attributes(self, *args):
        return None

    def overrideredirect(self, *args):
        return None

    def lift(self):
        return None

    def focus_set(self):
        return None

    def focus_force(self):
        return None

    def iconify(self):
        return None

    def start(self):
        return None

    def stop(self):
        return None


class _Var:
    def __init__(self, master=None, value=None, **kwargs):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


def _install_stubs() -> dict[str, types.ModuleType]:
    ctk = types.ModuleType("customtkinter")
    for name in (
        "CTk",
        "CTkToplevel",
        "CTkFrame",
        "CTkLabel",
        "CTkButton",
        "CTkTextbox",
        "CTkEntry",
        "CTkOptionMenu",
        "CTkSlider",
        "CTkSwitch",
        "CTkProgressBar",
        "CTkScrollableFrame",
        "CTkFont",
    ):
        setattr(ctk, name, type(name, (_Widget,), {}))
    ctk.set_appearance_mode = lambda *a, **k: None
    ctk.set_default_color_theme = lambda *a, **k: None
    ctk.set_widget_scaling = lambda *a, **k: None

    tk = types.ModuleType("tkinter")
    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = lambda *a, **k: ""
    filedialog.asksaveasfilename = lambda *a, **k: ""
    tk.filedialog = filedialog
    tk.StringVar = _Var
    tk.IntVar = _Var
    tk.BooleanVar = _Var
    tk.TclError = RuntimeError

    font = types.ModuleType("tkinter.font")
    font.Font = _Widget
    tk.font = font

    for name in ("Tk", "Toplevel", "Frame", "Label", "Button", "Entry", "Text", "Canvas", "Menu"):
        setattr(tk, name, type(name, (_Widget,), {}))

    return {
        "customtkinter": ctk,
        "tkinter": tk,
        "tkinter.filedialog": filedialog,
        "tkinter.font": font,
    }


@contextlib.contextmanager
def _gui_module():
    """Импортировать universum.gui на заглушках и вернуть модуль."""
    modules = _install_stubs()
    saved = {name: sys.modules.get(name) for name in modules}
    saved["universum.gui"] = sys.modules.get("universum.gui")
    sys.modules.update(modules)
    sys.modules.pop("universum.gui", None)
    try:
        import universum.gui as gui

        yield gui
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class _FakeResult:
    def __init__(self, pdf_path: Path) -> None:
        self.pdf_path = pdf_path


class _FakeOrchestrator:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.requests = []
        self.reloads = 0

    def reload_llm_config(self) -> None:
        self.reloads += 1

    def generate(self, request, progress=None, cancel_event=None):
        self.requests.append(request)
        if progress:
            progress("Пишу текст…")
        if self.mode == "latex":
            from universum.utils import LatexCompileError

            raise LatexCompileError("Не удалось скомпилировать PDF.", log="! Undefined control sequence.")
        return _FakeResult(Path("/tmp/universum-out.pdf"))


def _app(gui, mode: str = "ok"):
    app = gui.UniversumApp()
    app.orchestrator = _FakeOrchestrator(mode=mode)
    return app


def test_window_builds_and_collects_request():
    with _gui_module() as gui:
        from universum.config import DocType, Style

        app = _app(gui)
        assert app.request_box.get_value() == ""  # плейсхолдер — не текст

        app.request_box.set_value("Квантовая механика")
        app.style_menu.set(Style.COLORFUL.label_ru)
        app._on_style_changed()
        app.volume_slider.set(12)
        app._on_volume_changed()

        request = app._collect_request()
        assert request.request_text == "Квантовая механика"
        assert request.style is Style.COLORFUL
        assert request.doc_type is DocType.LECTURE
        assert request.volume.pages == 12
        assert request.file_path == ""


def test_test_type_switches_slider_to_questions():
    with _gui_module() as gui:
        from universum.config import DocType

        app = _app(gui)
        assert app.volume_slider._kwargs["from_"] == 1
        assert app.volume_slider._kwargs["to"] == 30

        app.doc_menu.set(DocType.TEST.label_ru)
        app._on_type_changed()
        assert app.volume_slider._kwargs["from_"] == 5
        assert app.volume_slider._kwargs["to"] == 50
        assert app.variants_slider is not None
        assert app.variants_row._visible is True

        request = app._collect_request()
        assert request.doc_type is DocType.TEST
        assert request.volume.pages == 5
        assert 5 <= request.volume.questions <= 50
        assert 2 <= request.volume.variants <= 6

        app.doc_menu.set(DocType.LECTURE.label_ru)
        app._on_type_changed()
        assert app.volume_slider._kwargs["from_"] == 1
        assert app.variants_row._visible is False


def test_empty_request_shows_warning_toast():
    with _gui_module() as gui:
        app = _app(gui)
        app._on_generate()
        assert app._worker is None
        assert any("Введите запрос" in toast.message for toast in gui.Toast._active)
        gui.Toast._active.clear()


def test_generation_flow_shows_result_and_enables_save():
    with _gui_module() as gui:
        app = _app(gui)
        app.request_box.set_value("Квантовая механика")
        app._on_generate()
        assert app._worker is not None
        app._worker.join(timeout=5)
        app._poll_queue()

        assert app._result_pdf == Path("/tmp/universum-out.pdf")
        assert app.save_btn._kwargs["state"] == "normal"
        assert app.generate_btn._kwargs["state"] == "normal"
        assert app.orchestrator.reloads >= 1
        assert app.orchestrator.requests[-1].request_text == "Квантовая механика"


def test_latex_error_opens_dialog_with_readonly_log():
    with _gui_module() as gui:
        app = _app(gui, mode="latex")
        app.request_box.set_value("Тест")
        app._on_generate()
        app._worker.join(timeout=5)
        app._poll_queue()

        dialog = app._dialogs[-1]
        assert dialog.details_box is not None
        assert "Undefined control sequence" in str(dialog.details_box._value)
        assert dialog.details_box._kwargs["state"] == "disabled"
        assert app.generate_btn._kwargs["state"] == "normal"


def test_cancel_sets_event():
    with _gui_module() as gui:
        app = _app(gui)
        app.request_box.set_value("Отмена")
        app._on_generate()
        app._worker.join(timeout=5)
        app._poll_queue()
        app._on_cancel()
        assert app._cancel_event.is_set()


def test_settings_window_is_modal_and_reused():
    with _gui_module() as gui:
        app = _app(gui)
        app._open_settings()
        first = app._settings_win
        assert first is not None
        app._open_settings()
        assert app._settings_win is first  # второе окно не создаётся


def test_volume_label_pluralisation():
    with _gui_module() as gui:
        assert gui._plural_pages(1) == "страница"
        assert gui._plural_pages(3) == "страницы"
        assert gui._plural_pages(25) == "страниц"


# ---------------------------------------------------------------------------
# Горячие клавиши: должны работать и на русской раскладке
# ---------------------------------------------------------------------------

class _Event:
    """Минимальное событие Tk: keysym/keycode/char, как в настоящем Tk."""

    def __init__(self, keysym: str = "", keycode: int | None = None, char: str = "") -> None:
        self.keysym = keysym
        self.keycode = keycode
        self.char = char


def _fire(widget, sequence: str, keysym: str = "", keycode: int | None = None, char: str = ""):
    handler = widget._bindings.get(sequence)
    assert handler is not None, f"нет привязки {sequence}"
    return handler(_Event(keysym, keycode, char))


def test_hotkey_letter_maps_russian_keysyms():
    with _gui_module() as gui:
        assert gui._hotkey_letter("Cyrillic_es") == "c"      # с
        assert gui._hotkey_letter("Cyrillic_em") == "v"      # м
        assert gui._hotkey_letter("Cyrillic_che") == "x"     # ч
        assert gui._hotkey_letter("Cyrillic_ef") == "a"      # ф
        assert gui._hotkey_letter("Cyrillic_ya") == "z"      # я
        assert gui._hotkey_letter("c") == "c"
        assert gui._hotkey_letter("F5") is None


def test_copy_hotkey_works_on_russian_layout():
    with _gui_module() as gui:
        app = _app(gui)
        box = app.request_box
        box.set_value("Квантовая механика")
        box._sel = ("1.0", "1.18")

        result = _fire(box, "<Control-KeyPress>", "Cyrillic_es")  # Ctrl+С
        assert result == "break"
        assert box._clipboard == "Квантовая механика"

        # латинская раскладка тоже работает
        box._clipboard = ""
        assert _fire(box, "<Control-KeyPress>", "c") == "break"
        assert box._clipboard == "Квантовая механика"


def test_paste_hotkey_works_on_russian_layout():
    with _gui_module() as gui:
        app = _app(gui)
        box = app.request_box
        box.set_value("")
        box._clipboard = "вставленный текст"
        assert _fire(box, "<Control-KeyPress>", "Cyrillic_em") == "break"  # Ctrl+М
        assert "вставленный текст" in str(box._value)


def test_select_all_and_cut_hotkeys():
    with _gui_module() as gui:
        app = _app(gui)
        box = app.request_box
        box.set_value("текст")
        assert _fire(box, "<Control-KeyPress>", "Cyrillic_ef") == "break"  # Ctrl+Ф
        assert box._sel == ("1.0", "end-1c")
        assert _fire(box, "<Control-KeyPress>", "Cyrillic_che") == "break"  # Ctrl+Ч
        assert box._clipboard == "текст"


def test_entry_hotkeys_work_on_russian_layout():
    with _gui_module() as gui:
        app = _app(gui)
        entry = gui.ctk.CTkEntry(app)
        gui.install_entry_hotkeys(entry)
        entry._value = "sk-secret-key"
        entry.select_range(0, "end")

        assert _fire(entry, "<Control-KeyPress>", "Cyrillic_es") == "break"
        assert entry._clipboard == "sk-secret-key"

        entry._clipboard = "sk-new"
        entry._sel = None
        assert _fire(entry, "<Control-KeyPress>", "Cyrillic_em") == "break"
        assert "sk-new" in str(entry._value)


def test_ctrl_enter_starts_generation():
    with _gui_module() as gui:
        app = _app(gui)
        app.request_box.set_value("Лекция по физике")
        # Ctrl+Enter привязан глобально (тег «all») и на самом текстовом поле
        assert app._bindings.get("all:<Control-Return>") is not None
        assert app.request_box._bindings.get("<Control-Return>") is not None
        assert app._on_generate_shortcut(_Event("Return")) == "break"
        assert app._worker is not None
        app._worker.join(timeout=5)
        app._poll_queue()
        assert app._result_pdf is not None


def test_test_volume_maps_questions_and_variants():
    with _gui_module() as gui:
        from universum.config import DocType

        app = _app(gui)
        app.doc_menu.set(DocType.TEST.label_ru)
        app._on_type_changed()
        app.volume_slider.set(7)
        assert app.variants_slider is not None
        app.variants_slider.set(5)
        app._on_volume_changed()

        request = app._collect_request()
        assert request.doc_type is DocType.TEST
        assert request.volume.questions == 7
        assert request.volume.variants == 5
        assert "7 вопросов" in app.volume_value._kwargs["text"]
        assert "5 вариантов" in app.volume_value._kwargs["text"]


def test_new_styles_are_available_in_menu():
    with _gui_module() as gui:
        from universum.config import Style

        app = _app(gui)
        values = app.style_menu._kwargs["values"]
        assert values == [style.label_ru for style in Style]
        assert "Без стиля" not in values
        for label in ("Строгий", "Красочный / креативный", "Неон", "Природа", "Современный минимализм"):
            assert label in values


def test_save_dialog_suggests_type_and_topic_name(tmp_path):
    """В диалоге сохранения сразу подставлено имя «Тип — Тема»."""
    with _gui_module() as gui:
        app = _app(gui)
        produced = tmp_path / "Тест — Квантовая механика.pdf"
        produced.write_bytes(b"%PDF-1.4 fake")
        app._result_pdf = produced

        captured: dict[str, object] = {}
        gui.filedialog.asksaveasfilename = lambda **kwargs: captured.update(kwargs) or ""
        app._on_save()

        assert captured["initialfile"] == "Тест — Квантовая механика.pdf"
        assert captured["defaultextension"] == ".pdf"


# ---------------------------------------------------------------------------
# Горячие клавиши: физический keycode (не зависит от раскладки)
# ---------------------------------------------------------------------------

_WIN_VK = {65: "a", 67: "c", 79: "o", 83: "s", 86: "v", 88: "x", 89: "y", 90: "z"}


def test_hotkey_letter_uses_physical_keycode_when_keysym_is_unusable():
    """На Windows с русской раскладкой Tk присылает keysym «??» — спасает keycode."""
    with _gui_module() as gui:
        gui._PLATFORM_KEYCODES = dict(_WIN_VK)  # как на Windows

        assert gui.hotkey_letter(_Event(keysym="??", keycode=67)) == "c"   # VK_C
        assert gui.hotkey_letter(_Event(keysym="??", keycode=86)) == "v"   # VK_V
        assert gui.hotkey_letter(_Event(keysym="??", keycode=65)) == "a"   # VK_A
        assert gui.hotkey_letter(_Event(keysym="??", keycode=90)) == "z"   # VK_Z
        assert gui.hotkey_letter(_Event(keysym="", keycode=88)) == "x"
        assert gui.hotkey_letter(_Event(keysym="F5", keycode=116)) is None


def test_hotkey_letter_understands_control_chars_and_cyrillic_chars():
    with _gui_module() as gui:
        gui._PLATFORM_KEYCODES = {}
        assert gui.hotkey_letter(_Event(keysym="", keycode=None, char="\x03")) == "c"
        assert gui.hotkey_letter(_Event(keysym="Cyrillic_em", keycode=None)) == "v"
        assert gui.hotkey_letter(_Event(keysym="", keycode=None, char="ф")) == "a"


def test_copy_paste_work_with_windows_keycodes():
    with _gui_module() as gui:
        gui._PLATFORM_KEYCODES = dict(_WIN_VK)
        app = _app(gui)
        box = app.request_box
        box.set_value("Квантовая механика")
        box._sel = ("1.0", "1.18")

        # русская раскладка: keysym невнятный, но keycode физический
        assert _fire(box, "<Control-KeyPress>", "??", 67) == "break"
        assert box._clipboard == "Квантовая механика"

        box.set_value("")
        box._clipboard = "вставлено"
        assert _fire(box, "<Control-KeyPress>", "??", 86) == "break"
        assert "вставлено" in str(box._value)

        assert _fire(box, "<Control-KeyPress>", "??", 65) == "break"  # Ctrl+A
        assert box._sel == ("1.0", "end-1c")


def test_ctrl_z_undoes_in_text_box():
    with _gui_module() as gui:
        gui._PLATFORM_KEYCODES = dict(_WIN_VK)
        app = _app(gui)
        box = app.request_box
        box.set_value("текст")
        assert _fire(box, "<Control-KeyPress>", "??", 90) == "break"  # Ctrl+Z
        assert str(box._value) == ""  # заглушка edit_undo очищает текст


def test_ctrl_enter_in_text_box_triggers_generation():
    with _gui_module() as gui:
        app = _app(gui)
        app.request_box.set_value("Лекция по физике")
        # привязка живёт на самом поле, чтобы не вставлять перевод строки
        assert _fire(app.request_box, "<Control-Return>") == "break"
        assert app._worker is not None
        app._worker.join(timeout=5)
        app._poll_queue()
        assert app._result_pdf is not None


def test_hotkeys_bound_to_inner_tk_widget_of_textbox():
    """Привязки ставятся на внутренний tk.Text (виджетный уровень)."""
    with _gui_module() as gui:
        app = _app(gui)
        inner = _Widget()
        app.request_box._textbox = inner
        gui.install_text_hotkeys(app.request_box, app._on_generate)
        assert "<Control-KeyPress>" in inner._bindings
        assert "<Control-Return>" in inner._bindings
        # undo включён — иначе Ctrl+Z в Text не работает вообще
        assert inner._kwargs.get("undo") is True


def test_cyrillic_keysyms_match_physical_keys():
    """Раскладка ЙЦУКЕН: каждая кириллическая клавиша = латинская на той же клавише."""
    with _gui_module() as gui:
        pairs = {
            "Cyrillic_es": "c", "Cyrillic_em": "v", "Cyrillic_che": "x",
            "Cyrillic_ef": "a", "Cyrillic_ya": "z", "Cyrillic_en": "y",
            "Cyrillic_te": "n", "Cyrillic_yeru": "s", "Cyrillic_shcha": "o",
            "Cyrillic_ka": "r", "Cyrillic_tse": "w", "Cyrillic_ve": "d",
            "Cyrillic_pe": "g", "Cyrillic_er": "h", "Cyrillic_o": "j",
            "Cyrillic_el": "k", "Cyrillic_de": "l", "Cyrillic_u": "e",
            "Cyrillic_ie": "t", "Cyrillic_sha": "i", "Cyrillic_ze": "p",
            "Cyrillic_ghe": "u", "Cyrillic_shorti": "q", "Cyrillic_softsign": "m",
            "Cyrillic_i": "b",
        }
        for keysym, letter in pairs.items():
            assert gui._KEYSYM_TO_LETTER[keysym] == letter, keysym
        # только латинские буквы и знаки — никакой кириллицы в таблице
        for value in gui._KEYSYM_TO_LETTER.values():
            assert value.isascii() and len(value) == 1
