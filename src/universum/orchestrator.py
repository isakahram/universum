"""Оркестратор: вся цепочка парсинг → поиск → LLM → LaTeX → PDF.

Один документ за раз. Поддерживает прогресс-колбэки и отмену.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import AppConfig, DocType, Style, Volume, get_config
from .file_parser import parse_file
from .latex_compiler import compile_with_repair
from .llm_client import LlmClient
from .prompts import SYSTEM_PROMPT, PromptContext, build_generation_prompt, detect_language
from .utils import UniversumError, extract_latex, get_logger, log_event, validate_latex
from .web_search import web_search

ProgressFn = Callable[[str], None]


@dataclass
class GenerationRequest:
    """Параметры генерации от GUI."""

    request_text: str = ""
    structure_text: str = ""
    file_path: str = ""
    doc_type: DocType = DocType.LECTURE
    style: Style = Style.STRICT
    volume: Volume = field(default_factory=Volume)
    use_web_search: bool = False


@dataclass
class GenerationResult:
    """Результат генерации."""

    pdf_path: Path
    latex_code: str
    sources: tuple[tuple[str, str], ...] = ()


class Orchestrator:
    """Координирует все этапы генерации."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self.logger = get_logger()
        self.llm = LlmClient(self.config.llm)

    def reload_llm_config(self) -> None:
        """Перечитать настройки нейросети (после изменения в GUI)."""
        from .config import LlmConfig, load_llm_settings

        settings = load_llm_settings()
        self.config = AppConfig(
            llm=LlmConfig.from_settings(settings),
            search=self.config.search,
            latex=self.config.latex,
            limits=self.config.limits,
            logs_dir=self.config.logs_dir,
            temp_dir=self.config.temp_dir,
        )
        self.llm = LlmClient(self.config.llm)

    def generate(
        self,
        req: GenerationRequest,
        *,
        progress: ProgressFn | None = None,
        cancel_event: threading.Event | None = None,
    ) -> GenerationResult:
        """Выполнить полную цепочку генерации.

        Args:
            req: Параметры от пользователя.
            progress: Колбэк прогресса (сообщение на русском).
            cancel_event: Событие отмены.

        Returns:
            Результат с путём к PDF.

        Raises:
            UniversumError: При любой ошибке (сообщение на русском).
        """
        def _p(msg: str) -> None:
            if progress:
                progress(msg)
            self.logger.info(msg)

        def _check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise UniversumError("Генерация отменена пользователем.")

        _check_cancel()
        request_text = (req.request_text or "").strip()
        structure_text = (req.structure_text or "").strip()
        if len(request_text) > self.config.limits.max_request_chars:
            request_text = request_text[: self.config.limits.max_request_chars]
        if len(structure_text) > self.config.limits.max_structure_chars:
            structure_text = structure_text[: self.config.limits.max_structure_chars]

        # 1. Парсинг файла (если есть)
        source_text = ""
        source_name = ""
        if req.file_path:
            _p("Чтение файла…")
            _check_cancel()
            source_name = Path(req.file_path).name
            source_text = parse_file(req.file_path, limits=self.config.limits)
            log_event(f"File parsed chars={len(source_text)}", preview=source_text[:80])

        if not request_text and not source_text:
            raise UniversumError("Введите запрос или загрузите файл — иначе не из чего делать документ.")

        # 2. Язык документа
        language = detect_language(request_text or source_text)

        # 3. Веб-поиск (опционально)
        sources: tuple[tuple[str, str], ...] = ()
        if req.use_web_search:
            _p("Поиск актуальных данных…")
            _check_cancel()
            query = request_text or source_text[:500]
            sources = tuple(web_search(query, config=self.config.search, progress=_p, cancel_event=cancel_event))
            if sources:
                _p(f"Найдено источников: {len(sources)}")

        # 4. Сборка промта
        _p("Подготовка промта…")
        _check_cancel()
        ctx = PromptContext(
            request_text=request_text,
            structure_text=structure_text,
            source_text=source_text,
            source_name=source_name,
            doc_type=req.doc_type,
            style=req.style,
            volume=req.volume,
            use_web_search=req.use_web_search,
            web_sources=sources,
            language=language,
        )
        user_prompt = build_generation_prompt(ctx)

        # 5. Генерация LaTeX через LLM
        _p("Генерация документа нейросетью…")
        _check_cancel()
        raw = self.llm.generate(SYSTEM_PROMPT, user_prompt, progress=_p, cancel_event=cancel_event)
        latex_code = extract_latex(raw)
        problems = validate_latex(latex_code)
        if problems:
            self.logger.warning("Generated LaTeX has issues: %s", problems)
        if not latex_code:
            raise UniversumError("Нейросеть не вернула LaTeX-код. Попробуйте ещё раз.")

        # 6. Компиляция с авто-ремонтом
        _p("Компиляция в PDF…")
        _check_cancel()

        def _llm_for_repair(system: str, user: str) -> str:
            return self.llm.generate(system, user, progress=_p, cancel_event=cancel_event)

        compiled = compile_with_repair(
            latex_code,
            _llm_for_repair,
            config=self.config.latex,
            progress=_p,
            cancel_event=cancel_event,
        )
        assert compiled.pdf_path is not None
        # Копируем PDF в стабильное место (tempdir может очиститься)
        out_dir = self.config.temp_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "universum_document.pdf"
        shutil.copyfile(compiled.pdf_path, out_path)
        _p("Готово!")
        log_event(f"Generation done pdf={out_path} bytes={out_path.stat().st_size}")
        return GenerationResult(pdf_path=out_path, latex_code=latex_code, sources=sources)
