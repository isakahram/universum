"""Графический интерфейс Universum (Tkinter, стандартная библиотека).

Главное окно: запрос, загрузка файла, структура, тип документа, стиль с превью,
объём, чекбокс веб-поиска, кнопка генерации, прогресс-бар, скачивание PDF.
Генерация идёт в фоновом потоке; ошибки показываются по-русски, лог — в отдельном окне.
"""

from __future__ import annotations

import queue
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import DocType, Style, Volume
from .orchestrator import GenerationRequest, Orchestrator
from .utils import LatexCompileError, UniversumError, get_logger

# Цвета превью стилей
_STYLE_COLORS: dict[Style, tuple[str, str, str]] = {
    Style.STRICT: ("#ffffff", "#000000", "#333333"),
    Style.COLORFUL: ("#ebf3fc", "#1e5aa0", "#e67e22"),
    Style.PLAIN: ("#ffffff", "#000000", "#000000"),
}


class StylePreview(tk.Canvas):
    """Мини-превью стиля: заголовок + строки текста + акценты."""

    def __init__(self, master, style: Style, width: int = 220, height: int = 110) -> None:
        super().__init__(master, width=width, height=height, bg="white", highlightthickness=1, highlightbackground="#cccccc")
        bg, title_c, accent = _STYLE_COLORS[style]
        # Фон
        self.create_rectangle(0, 0, width, height, fill=bg, outline="")
        if style == Style.COLORFUL:
            # Цветная плашка заголовка
            self.create_rectangle(8, 8, width - 8, 30, fill=title_c, outline="")
            self.create_text(width // 2, 19, text="Заголовок раздела", fill="white", font=("TkDefaultFont", 9, "bold"))
            # Блок-определение
            self.create_rectangle(8, 38, width - 8, 66, fill="white", outline=accent, width=2)
            self.create_text(14, 52, text="Ключевое определение", fill=title_c, font=("TkDefaultFont", 8, "bold"), anchor="w")
            for i, y in enumerate((74, 84, 94)):
                self.create_line(14, y, width - 14, y, fill="#999999", width=1)
        elif style == Style.STRICT:
            self.create_text(width // 2, 18, text="1  Заголовок раздела", fill=title_c, font=("TkDefaultFont", 9, "bold"))
            self.create_line(30, 28, width - 30, 28, fill="#000000", width=1)
            for y in (40, 52, 64, 76, 88, 100):
                self.create_line(14, y, width - 14, y, fill="#555555", width=1)
            # Номер страницы
            self.create_text(width // 2, height - 6, text="2", fill="#000000", font=("TkDefaultFont", 7))
        else:  # PLAIN
            self.create_text(14, 18, text="Заголовок раздела", fill=title_c, font=("TkDefaultFont", 9, "bold"), anchor="w")
            for y in (34, 48, 62, 76, 90, 102):
                self.create_line(14, y, width - 14, y, fill="#000000", width=1)


class UniversumApp(tk.Tk):
    """Главное окно приложения."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Universum — генератор учебных PDF")
        self.geometry("860x780")
        self.minsize(760, 680)
        self.logger = get_logger()
        self.orchestrator = Orchestrator()
        self._progress_queue: queue.Queue[str] = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._result_pdf: Path | None = None
        self._file_path: str = ""
        self._build_ui()
        self._poll_queue()

    # -- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # Заголовок
        ttk.Label(root, text="Universum — учебные материалы в PDF (LaTeX + нейросеть, бесплатно)", font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(0, 6))

        # 1. Запрос
        ttk.Label(root, text="1. Запрос: тема, аспекты, пожелания (или инструкция к файлу):").pack(anchor="w")
        self.request_text = tk.Text(root, height=5, wrap="word")
        self.request_text.pack(fill="x", pady=(2, 6))
        self.request_text.insert("1.0", "Например: Лекция по основам Python для новичков: переменные, циклы, функции. Простыми словами, с примерами из жизни.")

        # 2. Файл
        file_frame = ttk.Frame(root)
        file_frame.pack(fill="x", pady=(0, 6))
        ttk.Label(file_frame, text="2. Файл (необязательно): PDF, DOCX, DOC, TXT, PPTX, изображение/скан:").pack(anchor="w")
        row = ttk.Frame(file_frame)
        row.pack(fill="x", pady=(2, 0))
        ttk.Button(row, text="Выбрать файл…", command=self._choose_file).pack(side="left")
        ttk.Button(row, text="Сбросить", command=self._reset_file).pack(side="left", padx=(6, 0))
        self.file_label = ttk.Label(row, text="Файл не выбран — будет использован только запрос.", foreground="gray")
        self.file_label.pack(side="left", padx=(10, 0))

        # 3. Структура
        ttk.Label(root, text="3. Структура документа (необязательно — если пусто, будет стандартная для типа):").pack(anchor="w")
        self.structure_text = tk.Text(root, height=3, wrap="word")
        self.structure_text.pack(fill="x", pady=(2, 6))

        # 4. Тип документа
        ttk.Label(root, text="4. Тип документа:").pack(anchor="w")
        self.doctype_var = tk.StringVar(value=DocType.LECTURE.value)
        type_frame = ttk.Frame(root)
        type_frame.pack(fill="x", pady=(2, 6))
        for dt in DocType:
            ttk.Radiobutton(type_frame, text=dt.label_ru, value=dt.value, variable=self.doctype_var, command=self._on_type_changed).pack(side="left", padx=(0, 12))

        # 5. Стиль с превью
        ttk.Label(root, text="5. Стиль оформления (превью):").pack(anchor="w")
        self.style_var = tk.StringVar(value=Style.STRICT.value)
        style_frame = ttk.Frame(root)
        style_frame.pack(fill="x", pady=(2, 6))
        for st in Style:
            box = ttk.Frame(style_frame)
            box.pack(side="left", padx=(0, 14))
            ttk.Radiobutton(box, text=st.label_ru, value=st.value, variable=self.style_var).pack(anchor="w")
            StylePreview(box, st).pack(pady=(2, 0))

        # 6. Объём (динамический)
        ttk.Label(root, text="6. Объём:").pack(anchor="w")
        self.volume_frame = ttk.Frame(root)
        self.volume_frame.pack(fill="x", pady=(2, 6))
        self._build_volume_controls()

        # 7. Веб-поиск
        self.search_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(root, text="Искать актуальные данные в интернете (добавит список источников в PDF)", variable=self.search_var).pack(anchor="w", pady=(0, 6))

        # 8. Кнопки + прогресс
        btn_frame = ttk.Frame(root)
        btn_frame.pack(fill="x", pady=(2, 4))
        self.generate_btn = ttk.Button(btn_frame, text="Сгенерировать PDF", command=self._on_generate)
        self.generate_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btn_frame, text="Отмена", command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.save_btn = ttk.Button(btn_frame, text="Сохранить PDF…", command=self._on_save, state="disabled")
        self.save_btn.pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.pack(fill="x", pady=(4, 2))
        self.status_label = ttk.Label(root, text="Готово к работе. Введите запрос и нажмите «Сгенерировать PDF».", foreground="gray")
        self.status_label.pack(anchor="w")

        ttk.Label(
            root,
            text="Без API-ключей и оплаты. История не сохраняется. Первый запуск может скачать LaTeX-движок Tectonic (~30 МБ).",
            foreground="gray",
            font=("TkDefaultFont", 8),
        ).pack(anchor="w", pady=(6, 0))

    def _build_volume_controls(self) -> None:
        for child in self.volume_frame.winfo_children():
            child.destroy()
        dt = DocType(self.doctype_var.get())
        if dt == DocType.TEST:
            ttk.Label(self.volume_frame, text="Количество вопросов:").pack(side="left")
            self.questions_var = tk.IntVar(value=10)
            ttk.Spinbox(self.volume_frame, from_=1, to=50, width=5, textvariable=self.questions_var).pack(side="left", padx=(4, 14))
            ttk.Label(self.volume_frame, text="Вариантов в вопросе:").pack(side="left")
            self.variants_var = tk.IntVar(value=4)
            ttk.Spinbox(self.volume_frame, from_=2, to=6, width=5, textvariable=self.variants_var).pack(side="left", padx=(4, 0))
        else:
            ttk.Label(self.volume_frame, text="Количество страниц:").pack(side="left")
            self.pages_var = tk.IntVar(value=5)
            ttk.Spinbox(self.volume_frame, from_=1, to=30, width=5, textvariable=self.pages_var).pack(side="left", padx=(4, 0))

    def _on_type_changed(self) -> None:
        self._build_volume_controls()

    # -- Файл -------------------------------------------------------------

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
            self.file_label.config(text=f"Выбран: {Path(path).name}", foreground="black")

    def _reset_file(self) -> None:
        self._file_path = ""
        self.file_label.config(text="Файл не выбран — будет использован только запрос.", foreground="gray")

    # -- Генерация --------------------------------------------------------

    def _collect_request(self) -> GenerationRequest:
        dt = DocType(self.doctype_var.get())
        st = Style(self.style_var.get())
        if dt == DocType.TEST:
            volume = Volume(pages=5, questions=int(self.questions_var.get()), variants=int(self.variants_var.get()))
        else:
            volume = Volume(pages=int(self.pages_var.get()), questions=10, variants=4)
        return GenerationRequest(
            request_text=self.request_text.get("1.0", "end").strip(),
            structure_text=self.structure_text.get("1.0", "end").strip(),
            file_path=self._file_path,
            doc_type=dt,
            style=st,
            volume=volume,
            use_web_search=bool(self.search_var.get()),
        )

    def _on_generate(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        req = self._collect_request()
        if not req.request_text and not req.file_path:
            messagebox.showwarning("Universum", "Введите запрос или загрузите файл — иначе не из чего делать документ.")
            return
        self._cancel_event = threading.Event()
        self._result_pdf = None
        self.generate_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.save_btn.config(state="disabled")
        self.progress.start(12)
        self.status_label.config(text="Запуск генерации…", foreground="black")

        def _worker() -> None:
            try:
                result = self.orchestrator.generate(
                    req,
                    progress=lambda m: self._progress_queue.put(m),
                    cancel_event=self._cancel_event,
                )
                self._progress_queue.put(f"__DONE__{result.pdf_path}")
            except LatexCompileError as e:
                self._progress_queue.put(f"__LATEX_ERROR__{e.log}")
            except UniversumError as e:
                self._progress_queue.put(f"__ERROR__{e}")
            except Exception as e:  # pragma: no cover — защита от падений
                self.logger.exception("Unexpected error: %s", e)
                self._progress_queue.put(f"__ERROR__Произошла неожиданная ошибка: {e}")

        self._worker = threading.Thread(target=_worker, daemon=True)
        self._worker.start()

    def _on_cancel(self) -> None:
        self._cancel_event.set()
        self.status_label.config(text="Отмена запрошена…", foreground="gray")

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._progress_queue.get_nowait()
                if msg.startswith("__DONE__"):
                    self._result_pdf = Path(msg[len("__DONE__"):])
                    self.progress.stop()
                    self.generate_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.save_btn.config(state="normal")
                    self.status_label.config(text=f"Готово! PDF: {self._result_pdf.name}. Нажмите «Сохранить PDF…».", foreground="green")
                    messagebox.showinfo("Universum", "PDF готов! Нажмите «Сохранить PDF…», чтобы скачать файл.")
                elif msg.startswith("__LATEX_ERROR__"):
                    self.progress.stop()
                    self.generate_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.status_label.config(text="Ошибка компиляции LaTeX (см. лог).", foreground="red")
                    self._show_log_window(msg[len("__LATEX_ERROR__"):])
                elif msg.startswith("__ERROR__"):
                    self.progress.stop()
                    self.generate_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.status_label.config(text="Ошибка генерации.", foreground="red")
                    messagebox.showerror("Universum", msg[len("__ERROR__"):])
                else:
                    self.status_label.config(text=msg, foreground="black")
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _show_log_window(self, log_text: str) -> None:
        win = tk.Toplevel(self)
        win.title("Лог ошибки компиляции LaTeX")
        win.geometry("720x480")
        ttk.Label(win, text="Не удалось скомпилировать PDF даже после авто-исправлений. Лог компилятора:", padding=8).pack(anchor="w")
        text = tk.Text(win, wrap="word", font=("TkDefaultFont", 8))
        text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        text.insert("1.0", log_text or "(пустой лог)")
        text.config(state="disabled")
        ttk.Button(win, text="Закрыть", command=win.destroy).pack(pady=(0, 8))

    def _on_save(self) -> None:
        if self._result_pdf is None or not self._result_pdf.exists():
            messagebox.showwarning("Universum", "PDF ещё не готов. Сначала сгенерируйте документ.")
            return
        dest = filedialog.asksaveasfilename(
            title="Сохранить PDF",
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
            initialfile="universum_document.pdf",
        )
        if dest:
            try:
                shutil.copyfile(self._result_pdf, dest)
                messagebox.showinfo("Universum", f"PDF сохранён:\n{dest}")
            except Exception as e:
                messagebox.showerror("Universum", f"Не удалось сохранить файл: {e}")


def run() -> None:
    """Запустить GUI-приложение."""
    app = UniversumApp()
    app.mainloop()


if __name__ == "__main__":  # pragma: no cover
    run()
