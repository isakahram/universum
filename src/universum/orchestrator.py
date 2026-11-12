"""Оркестратор: вся цепочка парсинг → поиск → LLM → LaTeX → PDF.

Один документ за раз. Поддерживает прогресс-колбэки и отмену.
"""

from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import AppConfig, DocType, Style, Volume, get_config
from .file_parser import parse_file
from .latex_compiler import compile_latex, compile_with_repair
from .llm_client import LlmClient
from .latex_sanitizer import emergency_latex
from .prompts import (
    PLAN_SYSTEM_PROMPT,
    SECTION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    PromptContext,
    build_generation_prompt,
    build_plan_prompt,
    build_section_prompt,
    build_volume_fix_prompt,
    detect_language,
    wrap_document,
)
from .sections import (
    MIN_SECTION_WORDS,
    assemble_document,
    count_words,
    fallback_plan,
    insert_section,
    parse_plan,
    section_count,
    split_titles,
    target_words,
    trim_to_words,
)
from .utils import (
    LatexCompileError,
    UniversumError,
    count_variants,
    ensure_page_breaks,
    escape_latex_text,
    extract_document_body,
    get_logger,
    log_event,
    normalize_title_call,
    plural_ru,
    suggest_document_name,
    topic_from_request,
    validate_latex,
)
from .web_search import web_search

ProgressFn = Callable[[str], None]


# С какого объёма документ собирается по разделам (короткие — за один запрос):
# длинный ответ сервис обрывает по лимиту токенов, и LaTeX остаётся незакрытым.
_SECTIONAL_MIN_PAGES = 3

# Допустимое отклонение объёма от ползунка (доли).
_VOLUME_LOW = 0.65
_VOLUME_HIGH = 1.45


def _test_layout(body: str, doc_type: DocType) -> str:
    """Для тестов гарантировать новые страницы у вариантов и ключа ответов."""
    return ensure_page_breaks(body) if doc_type == DocType.TEST else body


def _strip_title_page(body: str) -> str:
    """Убрать из куска текста титул/оглавление/лишнюю преамбулу."""
    text = body or ""
    text = re.sub(r"\\makelecturetitle\s*(?:\[[^\]]*\])?\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\tableofcontents\b", "", text)
    text = re.sub(r"\\clearpage\b", "", text)
    return text.strip()


def _section_tail(chunks: list[str]) -> str:
    """Последние строки предыдущего раздела — для связности генерации."""
    if not chunks:
        return ""
    plain = " ".join(chunks[-1].split())
    return plain[-500:]


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
    simplified: bool = False  # PDF собран в «аварийном» режиме (без сложных команд)


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

        def _llm(system: str, user: str) -> str:
            return self.llm.generate(system, user, progress=_p, cancel_event=cancel_event)

        # Длинный документ собираем по разделам: так ответ не обрывается по лимиту
        # токенов, объём управляем и LaTeX почти никогда не ломается.
        sectional = req.doc_type != DocType.TEST and req.volume.pages >= _SECTIONAL_MIN_PAGES
        if sectional:
            body = self._generate_sectional(ctx, req, request_text, _llm, _p, _check_cancel)
            raw = body
        else:
            raw = _llm(SYSTEM_PROMPT, user_prompt)
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
        # Объём: подгоняем под ползунок, если документ собрался сильно короче/длиннее
        if not sectional and req.doc_type != DocType.TEST and body:
            body = self._fix_volume(ctx, body, user_prompt, _llm, _p, _check_cancel)
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

        simplified = False
        try:
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
        except LatexCompileError as err:
            # Последний рубеж: выкидываем всё, в чём не уверены (незнакомые команды,
            # окружения, определения), и пробуем собрать упрощённый документ.
            _p("Пробую упрощённую вёрстку…")
            _check_cancel()
            safe_body = emergency_latex(body)
            safe_code = wrap_document(
                safe_body, req.style, language=language, doc_type=req.doc_type
            )
            safe_result = compile_latex(
                safe_code, config=self.config.latex, progress=None, cancel_event=cancel_event
            )
            if not safe_result.success:
                raise err
            self.logger.warning("Emergency simplified compile succeeded")
            log_event("Emergency latex compile succeeded")
            compiled = safe_result
            latex_code = safe_code
            simplified = True
            _p("PDF собран в упрощённом режиме — часть оформления могла не сохраниться.")
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
        return GenerationResult(
            pdf_path=out_path, latex_code=latex_code, sources=sources, simplified=simplified
        )

    # -- объём и поэтапная генерация --------------------------------------

    def _generate_sectional(
        self,
        ctx: PromptContext,
        req: GenerationRequest,
        request_text: str,
        llm_call: Callable[[str, str], str],
        progress: Callable[[str], None],
        check_cancel: Callable[[], None],
    ) -> str:
        """Собрать длинный документ по разделам (план → разделы → склейка).

        Длинный ответ сервис обрывает по лимиту токенов: в обрыве остаются
        незакрытые окружения, и Tectonic падает. Поэтому документ генерируется
        маленькими кусками — каждый гарантированно доходит целиком.

        Args:
            ctx: Контекст промта.
            req: Запрос генерации.
            request_text: Текст запроса пользователя.
            llm_call: Функция вызова модели ``(system, user) -> text``.
            progress: Колбэк прогресса.
            check_cancel: Проверка отмены (бросает исключение).

        Returns:
            Тело документа (титул + разделы).
        """
        pages = req.volume.pages
        total_words = target_words(pages)
        wanted = section_count(pages)

        progress("План разделов…")
        check_cancel()
        titles: list[str] = []
        try:
            plan_raw = llm_call(PLAN_SYSTEM_PROMPT, build_plan_prompt(ctx, wanted))
            titles = parse_plan(plan_raw, limit=wanted)
        except UniversumError:
            raise
        except Exception as e:  # сеть/парсинг: план — не критичная часть
            self.logger.warning("Plan generation failed: %s", e)
        if len(titles) < 2:
            titles = fallback_plan(req.doc_type.value, pages, ctx.structure_text)
            log_event(f"Plan fallback used sections={len(titles)}")

        per_section = max(MIN_SECTION_WORDS, total_words // max(1, len(titles)))
        chunks: list[str] = []
        for index, title in enumerate(titles, 1):
            check_cancel()
            progress(f"Раздел {index} из {len(titles)}: {title}…")
            prompt = build_section_prompt(
                ctx,
                title=title,
                index=index,
                total=len(titles),
                words=per_section,
                outline=titles,
                previous_tail=_section_tail(chunks),
            )
            try:
                raw_part = llm_call(SECTION_SYSTEM_PROMPT, prompt)
            except UniversumError:
                raise
            except Exception as e:
                self.logger.warning("Section %s failed: %s", index, e)
                continue
            chunk = _strip_title_page(extract_document_body(raw_part))
            if not chunk.strip():
                continue
            if "\\section" not in chunk:
                chunk = f"\\section{{{title}}}\n\n" + chunk
            chunks.append(chunk)

        if not chunks:
            raise UniversumError("Нейросеть не вернула текст разделов. Попробуйте ещё раз.")

        theme = topic_from_request(request_text) or titles[0]
        title_call = r"\makelecturetitle{" + escape_latex_text(theme) + "}"
        with_toc = pages >= 4 and req.doc_type != DocType.HOMEWORK
        body = assemble_document(title_call, chunks, with_toc=with_toc)

        body = self._balance_sections(ctx, req, body, llm_call, progress, check_cancel)
        words = count_words(body)
        log_event(
            f"Sectional generation sections={len(chunks)} words={words} "
            f"target={total_words} pages={pages}"
        )
        return body

    def _balance_sections(
        self,
        ctx: PromptContext,
        req: GenerationRequest,
        body: str,
        llm_call: Callable[[str, str], str],
        progress: Callable[[str], None],
        check_cancel: Callable[[], None],
    ) -> str:
        """Довести объём собранного документа до ползунка.

        Если текста мало — добавляем содержательные разделы; если много —
        убираем целые разделы (не переписывая текст).

        Args:
            ctx: Контекст промта.
            req: Запрос генерации.
            body: Собранное тело.
            llm_call: Функция вызова модели.
            progress: Колбэк прогресса.
            check_cancel: Проверка отмены.

        Returns:
            Тело, близкое к целевому объёму.
        """
        total_words = target_words(ctx.volume.pages)
        words = count_words(body)
        if words > total_words * _VOLUME_HIGH:
            trimmed = trim_to_words(body, int(total_words * 1.35))
            if count_words(trimmed) < words:
                progress("Убираю лишний объём…")
                body = trimmed
                words = count_words(body)

        extras = (
            "Дополнительные примеры с разбором",
            "Типичные ошибки и как их избежать",
            "Практические задания с пояснениями",
        )
        for extra_title in extras:
            if words >= total_words * 0.85:
                break
            check_cancel()
            progress(f"Дополняю документ: {extra_title.lower()}…")
            missing = max(MIN_SECTION_WORDS, total_words - words)
            chunk_words = min(missing, max(MIN_SECTION_WORDS, total_words // 4))
            outline = [*split_titles(body), extra_title]
            prompt = build_section_prompt(
                ctx,
                title=extra_title,
                index=len(outline),
                total=len(outline),
                words=chunk_words,
                outline=outline,
                previous_tail="",
            )
            try:
                raw_part = llm_call(SECTION_SYSTEM_PROMPT, prompt)
            except UniversumError:
                raise
            except Exception as e:
                self.logger.warning("Extra section failed: %s", e)
                break
            chunk = _strip_title_page(extract_document_body(raw_part))
            if not chunk.strip():
                break
            if "\\section" not in chunk:
                chunk = f"\\section{{{extra_title}}}\n\n" + chunk
            body = insert_section(body, chunk)
            words = count_words(body)
        return body

    def _fix_volume(
        self,
        ctx: PromptContext,
        body: str,
        user_prompt: str,
        llm_call: Callable[[str, str], str],
        progress: Callable[[str], None],
        check_cancel: Callable[[], None],
    ) -> str:
        """Подогнать короткий документ (1–2 страницы) под выбранный объём.

        Args:
            ctx: Контекст промта.
            body: Тело от модели.
            user_prompt: Исходный промт (к нему добавляется правка объёма).
            llm_call: Функция вызова модели.
            progress: Колбэк прогресса.
            check_cancel: Проверка отмены.

        Returns:
            Тело — исходное или перегенерированное (что ближе к цели).
        """
        total_words = target_words(ctx.volume.pages)
        words = count_words(body)
        if total_words * _VOLUME_LOW <= words <= total_words * _VOLUME_HIGH:
            return body
        progress(f"Объём ≈{words} слов вместо {total_words} — прошу нейросеть исправить…")
        check_cancel()
        try:
            retry_prompt = user_prompt + build_volume_fix_prompt(ctx, words, total_words)
            retried_body = extract_document_body(llm_call(SYSTEM_PROMPT, retry_prompt))
        except UniversumError:
            raise
        except Exception as e:
            self.logger.warning("Volume fix failed: %s", e)
            return body
        if not retried_body:
            return body
        retried_words = count_words(retried_body)
        if abs(retried_words - total_words) < abs(words - total_words):
            log_event(f"Volume fix applied words={retried_words} target={total_words}")
            return retried_body
        return body

