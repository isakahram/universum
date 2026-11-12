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
from .prompts import (
    SYSTEM_PROMPT,
    PromptContext,
    build_generation_prompt,
    detect_language,
    wrap_document,
)
from .utils import (
    UniversumError,
    count_variants,
    ensure_page_breaks,
    plural_ru,
    extract_document_body,
    get_logger,
    log_event,
    normalize_title_call,
    suggest_document_name,
    validate_latex,
)
from .web_search import web_search

ProgressFn = Callable[[str], None]


def _test_layout(body: str, doc_type: DocType) -> str:
    """Для тестов гарантировать новые страницы у вариантов и ключа ответов."""
    return ensure_page_breaks(body) if doc_type == DocType.TEST else body


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
        body = extract_document_body(raw)

        # Тест: следим, чтобы вариантов было ровно столько, сколько выбрали
        if req.doc_type == DocType.TEST and body:
            expected = req.volume.variants
            found = count_variants(body)
            if found != expected:
                expected_text = f"{expected} {plural_ru(expected, 'вариант', 'варианта', 'вариантов')}"
                found_text = f"{found} {plural_ru(found, 'вариант', 'варианта', 'вариантов')}"
                _p(f"Получилось {found_text} вместо {expected_text} — прошу нейросеть переделать…")
                _check_cancel()
                retry_prompt = (
                    user_prompt
                    + f"\n\nИСПРАВЛЕНИЕ: в предыдущем ответе было {found_text}, а нужно ровно "
                    + f"{expected_text}. Верни тело заново: ровно {expected_text}, каждый с новой "
                    + f"страницы (\\clearpage) и заголовком «Вариант 1»…«Вариант {expected}», "
                    + f"по {req.volume.questions} вопросов в каждом, затем ключ ответов "
                    + "отдельным разделом с новой страницы."
                )
                retried = self.llm.generate(
                    SYSTEM_PROMPT, retry_prompt, progress=_p, cancel_event=cancel_event
                )
                retried_body = extract_document_body(retried)
                if retried_body:
                    retried_count = count_variants(retried_body)
                    # берём ответ, который ближе к запрошенному числу вариантов
                    if abs(retried_count - expected) < abs(found - expected):
                        body = retried_body
        # Модель отдаёт только тело; преамбулу и оформление задаёт шаблон стиля,
        # поэтому сломать пакеты/кодировку она не может.
        if not body:
            raise UniversumError("Нейросеть не вернула LaTeX-код. Попробуйте ещё раз.")
        # Титул приводим к безопасному виду: тема + только явно заданные данные,
        # тип документа подставляет преамбула (\universumdoctype).
        body = normalize_title_call(body, req.doc_type.label_ru, request_text)
        # ключ ответов и каждый вариант — с новой страницы (требование UX)
        body = _test_layout(body, req.doc_type)
        latex_code = wrap_document(body, req.style, language=language, doc_type=req.doc_type)
        problems = validate_latex(latex_code)
        if problems:
            self.logger.warning("Generated LaTeX has issues: %s", problems)

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
            # Ремонт возвращает тело — собираем документ заново с нашей преамбулой
            postprocess=lambda fixed_raw: wrap_document(
                _test_layout(
                    normalize_title_call(
                        extract_document_body(fixed_raw), req.doc_type.label_ru, request_text
                    ),
                    req.doc_type,
                ),
                req.style,
                language=language,
                doc_type=req.doc_type,
            ),
            prompt_view=extract_document_body,
        )
        assert compiled.pdf_path is not None
        # Копируем PDF в стабильное место (tempdir может очиститься)
        out_dir = self.config.temp_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Имя файла: «Тип документа — Тема» (тема из титула, иначе из запроса)
        safe_name = suggest_document_name(
            req.doc_type.label_ru, request_text=request_text, latex_code=latex_code
        )
        out_path = out_dir / safe_name
        shutil.copyfile(compiled.pdf_path, out_path)
        _p("Готово!")
        log_event(f"Generation done pdf={out_path} bytes={out_path.stat().st_size}")
        return GenerationResult(pdf_path=out_path, latex_code=latex_code, sources=sources)
