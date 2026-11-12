"""Парсинг входных файлов: PDF, DOCX, DOC, TXT, PPTX, изображения (OCR).

Все экстракторы лениво импортируют зависимости; если пакет не установлен —
бросается понятная ошибка с инструкцией по установке.
"""

from __future__ import annotations

from pathlib import Path

from .config import Limits, get_config
from .utils import FileParseError, log_event


def _check_size(path: Path, limits: Limits) -> None:
    size = path.stat().st_size
    if size > limits.max_upload_bytes:
        raise FileParseError(
            f"Файл слишком большой: {size / (1024 * 1024):.1f} МБ (лимит {limits.max_upload_bytes // (1024 * 1024)} МБ). "
            "Попробуйте файл поменьше."
        )


def _truncate(text: str, limits: Limits) -> str:
    if len(text) > limits.max_extracted_chars:
        return text[: limits.max_extracted_chars] + "\n\n[Текст обрезан для обработки]"
    return text


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise FileParseError("Для чтения PDF нужен пакет pypdf: pip install pypdf") from e
    try:
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        text = "\n\n".join(pages).strip()
        if not text:
            raise FileParseError("Не удалось извлечь текст из PDF (возможно, это скан без текстового слоя). Попробуйте загрузить изображение скана — OCR распознает его.")
        return text
    except FileParseError:
        raise
    except Exception as e:
        raise FileParseError(f"Не удалось прочитать PDF: {e}") from e


def _extract_docx(path: Path) -> str:
    try:
        import docx
    except ImportError as e:
        raise FileParseError("Для чтения DOCX нужен пакет python-docx: pip install python-docx") from e
    try:
        doc = docx.Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        # Таблицы
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        text = "\n".join(parts).strip()
        if not text:
            raise FileParseError("В DOCX не найден текст.")
        return text
    except FileParseError:
        raise
    except Exception as e:
        raise FileParseError(f"Не удалось прочитать DOCX: {e}") from e


def _extract_doc(path: Path) -> str:
    """DOC (старый Word): пробуем python-docx, затем olefile-текст, затем бинарный fallback."""
    # Попытка 1: python-docx (иногда .doc на деле .docx)
    try:
        import docx

        doc = docx.Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        if parts:
            return "\n".join(parts).strip()
    except Exception:
        pass
    # Попытка 2: olefile — вытащить потоки WordDocument и отфильтровать текст
    try:
        import olefile  # type: ignore

        if olefile.isOleFile(str(path)):
            ole = olefile.OleFileIO(str(path))
            data = b""
            for entry in ole.listdir():
                try:
                    data += ole.openstream(entry).read()
                except Exception:
                    continue
            ole.close()
            # Грубая эвристика: извлечь печатные строки UTF-16LE / Latin-1
            text = data.decode("utf-16le", errors="ignore")
            text = "".join(ch if (ch.isprintable() or ch in "\n\r\t") else " " for ch in text)
            lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 3]
            if lines:
                return "\n".join(lines)
    except ImportError:
        pass
    except Exception:
        pass
    raise FileParseError(
        "Не удалось прочитать .doc. Сохраните файл как .docx или .txt и загрузите снова, "
        "или установите olefile: pip install olefile"
    )


def _extract_txt(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            text = path.read_text(encoding=enc)
            if text.strip():
                return text.strip()
        except (UnicodeDecodeError, LookupError):
            continue
    raise FileParseError("Не удалось прочитать TXT (неизвестная кодировка).")


def _extract_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except ImportError as e:
        raise FileParseError("Для чтения PPTX нужен пакет python-pptx: pip install python-pptx") from e
    try:
        prs = Presentation(str(path))
        parts: list[str] = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    t = shape.text.strip()
                    if t:
                        parts.append(t)
                if shape.has_table:
                    for row in shape.table.rows:
                        parts.append(" | ".join(cell.text.strip() for cell in row.cells))
            parts.append("--- слайд ---")
        text = "\n".join(parts).strip()
        if not text or text == "--- слайд ---":
            raise FileParseError("В PPTX не найден текст.")
        return text
    except FileParseError:
        raise
    except Exception as e:
        raise FileParseError(f"Не удалось прочитать PPTX: {e}") from e


def _extract_image_ocr(path: Path) -> str:
    """OCR изображения через Tesseract (pytesseract)."""
    try:
        from PIL import Image
    except ImportError as e:
        raise FileParseError("Для OCR нужен пакет Pillow: pip install Pillow") from e
    try:
        import pytesseract
    except ImportError as e:
        raise FileParseError(
            "Для распознавания изображений нужен pytesseract + системный Tesseract: "
            "pip install pytesseract и https://github.com/tesseract-ocr/tesseract"
        ) from e
    try:
        image = Image.open(str(path))
        # Tesseract лучше работает с русским+английским
        text = pytesseract.image_to_string(image, lang="rus+eng").strip()
        if not text:
            # Попробовать без указания языка
            text = pytesseract.image_to_string(image).strip()
        if not text:
            raise FileParseError("OCR не распознал текст на изображении. Попробуйте более чёткий скан.")
        return text
    except FileParseError:
        raise
    except Exception as e:
        msg = str(e)
        if "tesseract" in msg.lower() or "not found" in msg.lower():
            raise FileParseError(
                "Системный Tesseract не найден. Установите его: https://github.com/tesseract-ocr/tesseract "
                "или загрузите текстовый файл вместо изображения."
            ) from e
        raise FileParseError(f"Ошибка OCR: {e}") from e


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def parse_file(path_str: str, *, limits: Limits | None = None) -> str:
    """Распарсить файл и вернуть текст.

    Args:
        path_str: Путь к файлу.
        limits: Лимиты (по умолчанию из конфигурации).

    Returns:
        Извлечённый текст (обрезанный до лимита).

    Raises:
        FileParseError: При ошибке чтения.
    """
    lim = limits or get_config().limits
    path = Path(path_str)
    if not path.exists():
        raise FileParseError(f"Файл не найден: {path.name}")
    ext = path.suffix.lower()
    if ext not in lim.allowed_extensions:
        raise FileParseError(
            f"Неподдерживаемый формат '{ext}'. Поддерживаются: {', '.join(lim.allowed_extensions)}"
        )
    _check_size(path, lim)
    log_event(f"Parse file ext={ext} name={path.name} bytes={path.stat().st_size}")

    if ext == ".pdf":
        text = _extract_pdf(path)
    elif ext == ".docx":
        text = _extract_docx(path)
    elif ext == ".doc":
        text = _extract_doc(path)
    elif ext == ".txt":
        text = _extract_txt(path)
    elif ext == ".pptx":
        text = _extract_pptx(path)
    elif ext in _IMAGE_EXTS:
        text = _extract_image_ocr(path)
    else:  # pragma: no cover — защищено проверкой выше
        raise FileParseError(f"Неподдерживаемый формат '{ext}'.")
    return _truncate(text, lim)
