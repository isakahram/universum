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
    """Минимальный «виджет»: запоминает аргументы и умеет pack/configure/get/set."""

    def __init__(self, master=None, **kwargs):
        self._kwargs = dict(kwargs)
        self._children: list[_Widget] = []
        self._value: object = ""
        self._visible = False
        self._alive = True
        self._command = kwargs.get("command")
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

    def bind(self, *args, **kwargs):
        return "binding"

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

    def clipboard_clear(self):
        return None

    def clipboard_append(self, *args):
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
