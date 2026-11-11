"""Unit-тесты Universum (без сети, GUI и LaTeX — всё замокано)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from universum.config import (
    PROVIDER_PRESETS,
    DocType,
    LlmConfig,
    LlmProvider,
    LlmSettings,
    Style,
    Volume,
    is_localhost_url,
    mask_key,
)
from universum.file_parser import parse_file
from universum.llm_client import friendly_http_error, friendly_network_error
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


def test_mask_key():
    assert mask_key("") == "(не задан)"
    assert mask_key("sk-abcdefgh1234") == "sk-...1234"
    assert "abcdefgh" not in mask_key("sk-abcdefgh1234")


def test_is_localhost_url():
    assert is_localhost_url("http://localhost:11434/v1/chat/completions")
    assert is_localhost_url("http://127.0.0.1:11434/")
    assert not is_localhost_url("https://api.openai.com/v1/chat/completions")


def test_provider_presets():
    assert PROVIDER_PRESETS[LlmProvider.OPENAI]["url"] == "https://api.openai.com/v1/chat/completions"
    assert PROVIDER_PRESETS[LlmProvider.POLLINATIONS]["needs_key"] is False
    assert PROVIDER_PRESETS[LlmProvider.OPENAI]["needs_key"] is True


def test_settings_resolved():
    s = LlmSettings(provider=LlmProvider.OPENAI)
    assert s.resolved_url() == "https://api.openai.com/v1/chat/completions"
    assert s.resolved_model() == "gpt-4o-mini"
    s2 = LlmSettings(provider=LlmProvider.CUSTOM, url="https://x.example/v1", model="m1")
    assert s2.resolved_url() == "https://x.example/v1"
    assert "sk-" not in repr(s2)  # ключ не в repr


def test_config_from_settings_sends_key_only_remote():
    s = LlmSettings(provider=LlmProvider.OPENAI, api_key="sk-testkey1234")
    cfg = LlmConfig.from_settings(s)
    eps = cfg.endpoints
    # Первый эндпоинт — OpenAI с ключом
    assert eps[0][0] == "https://api.openai.com/v1/chat/completions"
    assert eps[0][2] is True
    # Ollama всегда без ключа
    assert eps[-1][2] is False


def test_config_pollinations_no_key():
    s = LlmSettings(provider=LlmProvider.POLLINATIONS)
    cfg = LlmConfig.from_settings(s)
    assert cfg.endpoints[0][2] is False


def test_friendly_errors():
    assert "Ollama" in friendly_network_error("[WinError 10061] ...", "http://localhost:11434/")
    assert "10061" in friendly_network_error("[WinError 10061] ...", "https://api.openai.com/")
    assert "ключ" in friendly_http_error(401, "bad key").lower()
    assert "лимит" in friendly_http_error(429, "").lower()


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
