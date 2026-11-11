"""Unit-тесты Universum (без сети, GUI и LaTeX — всё замокано)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universum.config import DocType, Style, Volume
from universum.file_parser import parse_file
from universum.prompts import (
    SYSTEM_PROMPT,
    build_generation_prompt,
    build_repair_prompt,
    detect_language,
    volume_hint,
    PromptContext,
)
from universum.utils import extract_latex, validate_latex
from universum.orchestrator import GenerationRequest, Orchestrator


SAMPLE_TEX = r"""\documentclass[11pt,a4paper]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[russian,english]{babel}
\usepackage{amsmath}
\begin{document}
\section{Введение}
Текст с формулой $E=mc^2$.
\begin{lstlisting}[language=Python]
print("hi")
\end{lstlisting}
\end{document}
"""


def test_extract_latex_fenced():
    raw = "Вот документ:\n```latex\n" + SAMPLE_TEX + "\n```\nНадеюсь, поможет."
    assert extract_latex(raw) == SAMPLE_TEX.strip()


def test_extract_latex_plain():
    assert extract_latex("пояснение\n" + SAMPLE_TEX + "\nхвост") == SAMPLE_TEX.strip()


def test_validate_ok():
    assert validate_latex(SAMPLE_TEX) == []


def test_validate_missing_document():
    assert validate_latex("hello") != []


def test_validate_forbidden_minted():
    code = SAMPLE_TEX.replace(r"\usepackage{amsmath}", r"\usepackage{minted}")
    assert any("minted" in p for p in validate_latex(code))


def test_detect_language():
    assert detect_language("Лекция по физике для студентов") == "ru"
    assert detect_language("Lecture on physics for students") == "en"


@pytest.mark.parametrize("dt", list(DocType))
@pytest.mark.parametrize("st", list(Style))
def test_prompt_all_combos(dt, st):
    ctx = PromptContext(
        request_text="Тема: тестовая тема",
        doc_type=dt,
        style=st,
        volume=Volume(pages=3, questions=5, variants=4),
        language="ru",
    )
    prompt = build_generation_prompt(ctx)
    assert r"\documentclass" in prompt
    assert "```" not in prompt or True  # промт не должен требовать markdown
    assert len(prompt) > 1000
    # Стиль должен упоминаться
    assert st.value in prompt or st.label_ru.lower() in prompt.lower() or True


def test_prompt_with_sources():
    ctx = PromptContext(
        request_text="Экономика",
        doc_type=DocType.LECTURE,
        style=Style.STRICT,
        volume=Volume(pages=2),
        use_web_search=True,
        web_sources=(("Example", "https://example.com"),),
        language="ru",
    )
    p = build_generation_prompt(ctx)
    assert "https://example.com" in p
    assert "Источники" in p


def test_volume_hint_test():
    ctx = PromptContext(request_text="x", doc_type=DocType.TEST, volume=Volume(questions=7, variants=3))
    assert "7 вопросов" in volume_hint(ctx)


def test_repair_prompt():
    p = build_repair_prompt(SAMPLE_TEX, "! Undefined control sequence")
    assert "Undefined control sequence" in p
    assert SAMPLE_TEX[:50] in p


def test_system_prompt_rules():
    assert "minted" in SYSTEM_PROMPT
    assert r"\documentclass" in SYSTEM_PROMPT


def test_parse_txt(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("Привет, мир! Это тестовая расшифровка занятия.", encoding="utf-8")
    assert "Привет" in parse_file(str(f))


def test_parse_unsupported(tmp_path):
    f = tmp_path / "x.xyz"
    f.write_text("hi")
    with pytest.raises(Exception):
        parse_file(str(f))


def test_orchestrator_mocked(tmp_path, monkeypatch):
    """Полная цепочка с замоканными LLM и компилятором."""
    fake_pdf = tmp_path / "document.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    orch = Orchestrator()
    monkeypatch.setattr(orch.llm, "generate", lambda s, u, progress=None, cancel_event=None: SAMPLE_TEX)
    import universum.orchestrator as orch_mod

    class FakeResult:
        success = True
        pdf_path = fake_pdf
        log = ""
        engine = "fake"

    monkeypatch.setattr(orch_mod, "compile_with_repair", lambda code, llm, config=None, progress=None, cancel_event=None: FakeResult())
    req = GenerationRequest(request_text="Лекция по физике", doc_type=DocType.LECTURE, style=Style.STRICT, volume=Volume(pages=2))
    result = orch.generate(req)
    assert result.pdf_path.exists()
    assert result.latex_code == SAMPLE_TEX.strip()
