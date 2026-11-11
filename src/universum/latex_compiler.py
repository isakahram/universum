"""Компиляция LaTeX в PDF: Tectonic (авто-скачивание) / xelatex / pdflatex / lualatex.

Tectonic — один бинарь с GitHub, не требует установки TeX Live.
Если движок не найден, пробуем автоматически скачать Tectonic под вашу ОС.

Устойчивость: весь вывод процесса и любые внешние данные приводятся к строке
через ``_text()`` (``None`` -> ``""``), поэтому ошибки вида
``can only concatenate str (not "NoneType") to str`` невозможны, а любые
непредвиденные исключения превращаются в ``CompileResult`` с логом,
а не в падение GUI.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
import traceback
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import LatexConfig, get_config
from .utils import LatexCompileError, get_logger, log_event, sanitize_latex

ProgressFn = Callable[[str], None]


def _text(value: object | None) -> str:
    """Привести вывод процесса/лог к строке (``None`` -> пустая строка).

    ``subprocess`` может вернуть ``None`` в ``stdout``/``stderr`` (например,
    если потоки были перенаправлены снаружи, при запуске из GUI-сборки без
    консоли или при нестандартном ``Popen``). Прямая конкатенация такого
    значения даёт ``TypeError: can only concatenate str (not "NoneType") to str``.

    Args:
        value: Любое значение из внешнего источника.

    Returns:
        Безопасная строка (никогда не ``None``).
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@dataclass
class CompileResult:
    """Результат компиляции."""

    success: bool
    pdf_path: Path | None
    log: str = ""
    engine: str = ""

    def __post_init__(self) -> None:
        """Нормализовать поля: лог всегда строка, движок всегда строка."""
        self.log = _text(self.log)
        self.engine = _text(self.engine)


def find_engine(preference: tuple[str, ...] = ("tectonic", "xelatex", "pdflatex", "lualatex")) -> str | None:
    """Найти доступный LaTeX-движок.

    Args:
        preference: Порядок предпочтения.

    Returns:
        Имя движка или None.
    """
    for name in preference:
        try:
            if shutil.which(name):
                return name
        except Exception:  # pragma: no cover — защита от странного PATH
            continue
    return None


def _tectonic_asset() -> tuple[str, str] | None:
    """Определить asset Tectonic для текущей платформы (имя файла, URL)."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    # Релизы: https://github.com/tectonic-typesetting/tectonic/releases
    # Имена вида: tectonic-0.15.0-x86_64-unknown-linux-gnu.tar.gz
    api = "https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "Universum/1.0", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            import json

            data = json.loads(_text(resp.read().decode("utf-8", errors="replace")))
        assets = data.get("assets") or []
        if system == "linux":
            want = "x86_64-unknown-linux-gnu" if "x86_64" in machine or "amd64" in machine else "aarch64-unknown-linux-gnu"
        elif system == "darwin":
            want = "x86_64-apple-darwin" if "x86_64" in machine else "aarch64-apple-darwin"
        elif system == "windows":
            want = "x86_64-pc-windows-msvc"
        else:
            return None
        for a in assets:
            name = _text(a.get("name"))
            if want in name and name.endswith((".tar.gz", ".zip")):
                url = _text(a.get("browser_download_url"))
                if url:
                    return name, url
                return None
    except Exception as e:
        get_logger().warning("Tectonic release lookup failed: %s", e)
    return None


def ensure_tectonic(progress: ProgressFn | None = None) -> Path | None:
    """Скачать Tectonic в temp-папку, если движка нет. Вернуть путь к бинарю или None."""
    logger = get_logger()
    found = _tectonic_asset()
    if not found:
        logger.warning("Could not determine Tectonic asset for this platform")
        return None
    name, url = found
    if progress:
        progress("Скачивание LaTeX-движка Tectonic (первый запуск, ~30 МБ)…")
    dest_dir = Path(get_config().temp_dir) / "tectonic"
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / name
    is_windows = platform.system().lower() == "windows"
    binary = dest_dir / ("tectonic.exe" if is_windows else "tectonic")
    if binary.exists():
        return binary
    try:
        log_event(f"Downloading tectonic {name}")
        req = urllib.request.Request(url, headers={"User-Agent": "Universum/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(archive, "wb") as f:
            shutil.copyfileobj(resp, f)
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(dest_dir)
        else:
            import tarfile

            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(dest_dir)
        # Найти бинарь внутри (может быть во вложенной папке)
        for c in dest_dir.rglob("tectonic*"):
            if c.is_file() and c.name in ("tectonic", "tectonic.exe"):
                if not is_windows:
                    c.chmod(0o755)
                return c
        logger.warning("Tectonic archive extracted but binary not found in %s", dest_dir)
        return None
    except Exception as e:
        logger.warning("Tectonic download failed: %s", e)
        return None


def _engine_command(engine: str | None, executable: str | None = None) -> list[str] | None:
    """Собрать команду запуска движка.

    Args:
        engine: Имя движка (tectonic/xelatex/pdflatex/lualatex) или None.
        executable: Явный путь к бинарю (для авто-скачанного Tectonic).

    Returns:
        Список аргументов команды или None, если движок неизвестен.
    """
    exe = _text(executable) or _text(engine)
    if not exe:
        return None
    kind = _text(engine) or ("tectonic" if "tectonic" in Path(exe).name.lower() else "")
    if kind == "tectonic" or "tectonic" in Path(exe).name.lower():
        return [exe, "-X", "compile", "--keep-logs", "document.tex"]
    if kind in ("xelatex", "pdflatex", "lualatex"):
        return [exe, "-interaction=nonstopmode", "-halt-on-error", "document.tex"]
    return None


def compile_latex(
    latex_code: str,
    *,
    config: LatexConfig | None = None,
    progress: ProgressFn | None = None,
    cancel_event=None,
) -> CompileResult:
    """Скомпилировать LaTeX-код в PDF.

    Args:
        latex_code: Полный LaTeX-документ.
        config: Конфигурация компиляции.
        progress: Колбэк прогресса.
        cancel_event: Событие отмены.

    Returns:
        CompileResult (success=False + log при ошибке). Функция не выбрасывает
        исключений: любая непредвиденная ошибка попадает в ``log``.
    """
    try:
        return _compile_latex_impl(
            _text(latex_code),
            config=config,
            progress=progress,
            cancel_event=cancel_event,
        )
    except Exception as e:  # pragma: no cover — последний предохранитель
        get_logger().exception("Unexpected LaTeX compile error: %s", e)
        return CompileResult(
            success=False,
            pdf_path=None,
            log=f"Непредвиденная ошибка при компиляции: {e}\n\n{traceback.format_exc()}",
            engine="",
        )


def _compile_latex_impl(
    latex_code: str,
    *,
    config: LatexConfig | None = None,
    progress: ProgressFn | None = None,
    cancel_event=None,
) -> CompileResult:
    """Внутренняя реализация компиляции (см. :func:`compile_latex`)."""
    cfg = config or get_config().latex
    logger = get_logger()
    # Последняя страховка: внутренние @-макросы (\\@, \\makeatletter) ломают
    # Tectonic с «You can't use \spacefactor in internal vertical mode».
    latex_code = sanitize_latex(latex_code)
    workdir = Path(tempfile.mkdtemp(prefix="universum_latex_"))
    tex_file = workdir / "document.tex"
    tex_file.write_text(latex_code, encoding="utf-8")
    log_event(f"LaTeX compile workdir={workdir} chars={len(latex_code)}")

    engine = find_engine(tuple(getattr(cfg, "engine_preference", ()) or ()))
    engine_cmd = _engine_command(engine)
    if engine_cmd is None:
        # Попытка авто-скачивания Tectonic
        tectonic_path = ensure_tectonic(progress)
        downloaded = tectonic_path is not None
        engine_cmd = _engine_command(
            "tectonic" if downloaded else None,
            str(tectonic_path) if downloaded else None,
        )
        if engine_cmd is None:
            log = (
                "LaTeX-движок не найден (tectonic/xelatex/pdflatex/lualatex). "
                "Установите TeX Live или MiKTeX, либо проверьте интернет для авто-скачивания Tectonic."
            )
            return CompileResult(success=False, pdf_path=None, log=log, engine="")
        engine = "tectonic"

    engine_name = _text(engine) or Path(engine_cmd[0]).name
    if progress:
        progress(f"Компиляция LaTeX ({engine_name})…")
    # Для xelatex/pdflatex/lualatex может понадобиться 2 прохода (оглавление/ссылки)
    passes = 2 if engine_name in ("xelatex", "pdflatex", "lualatex") else 1
    log_parts: list[str] = []

    try:
        for _i in range(passes):
            if cancel_event is not None and cancel_event.is_set():
                return CompileResult(success=False, pdf_path=None, log="Компиляция отменена пользователем.", engine=engine_name)
            proc = subprocess.run(
                engine_cmd,
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,  # без консоли stdin может отсутствовать
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(getattr(cfg, "compile_timeout_sec", 120) or 120),
            )
            # ВАЖНО: stdout/stderr могут быть None — приводим к строке,
            # иначе TypeError: can only concatenate str (not "NoneType") to str
            out = _text(getattr(proc, "stdout", None))
            err = _text(getattr(proc, "stderr", None))
            if out:
                log_parts.append(out)
            if err:
                log_parts.append(err)
            returncode = getattr(proc, "returncode", None)
            if returncode not in (0, None):
                log_parts.append(f"[компилятор завершился с кодом {returncode}]")
                break
    except subprocess.TimeoutExpired:
        log_parts.append(f"Таймаут компиляции ({int(getattr(cfg, 'compile_timeout_sec', 120) or 120)} с).")
        return CompileResult(success=False, pdf_path=None, log="\n".join(p for p in log_parts if p), engine=engine_name)
    except FileNotFoundError:
        return CompileResult(
            success=False,
            pdf_path=None,
            log=f"Движок '{engine_name}' не найден. Установите TeX Live/MiKTeX или разрешите авто-скачивание Tectonic.",
            engine=engine_name,
        )
    except OSError as e:
        log_parts.append(f"Ошибка запуска движка '{engine_name}': {e}")
        return CompileResult(success=False, pdf_path=None, log="\n".join(p for p in log_parts if p), engine=engine_name)
    except Exception as e:
        log_parts.append(f"Ошибка запуска: {e}")
        logger.exception("LaTeX engine run failed")
        return CompileResult(success=False, pdf_path=None, log="\n".join(p for p in log_parts if p), engine=engine_name)

    full_log = "\n".join(part for part in log_parts if part).strip()
    pdf_path = workdir / "document.pdf"
    try:
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            log_event(f"LaTeX compile success engine={engine_name} pdf_bytes={pdf_path.stat().st_size}")
            return CompileResult(success=True, pdf_path=pdf_path, log=full_log, engine=engine_name)
        # Иногда движок кладёт PDF с другим именем — ищем любой .pdf
        pdfs = sorted(p for p in workdir.glob("*.pdf") if p.is_file() and p.stat().st_size > 0)
    except OSError as e:  # pragma: no cover — защита от ошибок ФС
        return CompileResult(success=False, pdf_path=None, log=f"{full_log}\nНе удалось проверить PDF: {e}", engine=engine_name)
    if pdfs:
        return CompileResult(success=True, pdf_path=pdfs[0], log=full_log, engine=engine_name)
    return CompileResult(
        success=False,
        pdf_path=None,
        log=full_log or f"PDF не создан (движок {engine_name} не вернул вывода).",
        engine=engine_name,
    )


def compile_with_repair(
    latex_code: str,
    llm_generate,  # Callable[[str, str], str] — (system, user) -> text
    *,
    config: LatexConfig | None = None,
    progress: ProgressFn | None = None,
    cancel_event=None,
) -> CompileResult:
    """Компиляция с авто-ремонтом через LLM (до N попыток).

    Args:
        latex_code: Исходный LaTeX.
        llm_generate: Функция вызова LLM (system, user) -> ответ.
        config: Конфигурация.
        progress: Колбэк прогресса.
        cancel_event: Событие отмены.

    Returns:
        Успешный CompileResult или последний неудачный (с логом).

    Raises:
        LatexCompileError: Если PDF не удалось собрать после всех попыток
            или компиляция отменена. ``log`` всегда строка (не ``None``).
    """
    from .prompts import SYSTEM_PROMPT, build_repair_prompt
    from .utils import extract_latex, validate_latex

    cfg = config or get_config().latex
    current = _text(latex_code)
    last: CompileResult | None = None
    max_repairs = int(getattr(cfg, "max_repair_attempts", 3) or 0)
    for attempt in range(max_repairs + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise LatexCompileError("Компиляция отменена пользователем.", log="")
        if progress:
            progress(f"Компиляция LaTeX (попытка {attempt + 1})…")
        result = compile_latex(current, config=cfg, progress=None, cancel_event=cancel_event)
        last = result
        if result.success:
            return result
        if attempt >= max_repairs:
            break
        # Авто-ремонт
        if progress:
            progress(f"Исправление ошибок LaTeX (попытка {attempt + 1}/{max_repairs})…")
        log_event(f"LaTeX repair attempt={attempt + 1}")
        try:
            repair_prompt = build_repair_prompt(current, _text(result.log))
            fixed_raw = _text(llm_generate(SYSTEM_PROMPT, repair_prompt))
            fixed = extract_latex(fixed_raw)
            problems = validate_latex(fixed)
            if problems:
                get_logger().warning("Repair produced invalid LaTeX: %s", problems)
            if not fixed:
                continue
            current = fixed
        except Exception as e:
            get_logger().warning("Repair LLM call failed: %s", e)
            continue
    log = _text(last.log if last is not None else "") or "Лог компилятора пуст — движок не вернул вывода."
    raise LatexCompileError(
        "Не удалось скомпилировать PDF после авто-исправлений. Лог ошибки — ниже.",
        log=log,
    )
