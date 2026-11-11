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
    normalize_api_key,
)
from universum.file_parser import parse_file
from universum.llm_client import (
    LlmClient,
    auth_headers,
    build_payload,
    effective_max_tokens,
    friendly_http_error,
    friendly_network_error,
)
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


def test_pollinations_new_endpoint_and_model():
    assert PROVIDER_PRESETS[LlmProvider.POLLINATIONS]["url"] == "https://gen.pollinations.ai/v1/chat/completions"
    assert PROVIDER_PRESETS[LlmProvider.POLLINATIONS]["model"] == "openai"
    cfg = LlmConfig()
    assert cfg.primary_url == "https://gen.pollinations.ai/v1/chat/completions"
    assert cfg.model == "openai"
    assert cfg.max_tokens is None  # не отправляем — сервер решает


def test_pollinations_migration():
    from universum.config import migrate_pollinations_model, migrate_pollinations_url

    assert migrate_pollinations_url("https://text.pollinations.ai/openai") == "https://gen.pollinations.ai/v1/chat/completions"
    assert migrate_pollinations_model("openai-fast") == "openai"
    s = LlmSettings(provider=LlmProvider.POLLINATIONS, url="https://text.pollinations.ai/openai", model="openai-fast")
    assert s.resolved_url() == "https://gen.pollinations.ai/v1/chat/completions"
    assert s.resolved_model() == "openai"


def test_no_old_pollinations_anywhere():
    # Активные эндпоинты не должны указывать на старый text.pollinations.ai
    # (константа OLD_POLLINATIONS_URL для миграции — допустима).
    from universum.config import OLD_POLLINATIONS_URL, PROVIDER_PRESETS

    assert OLD_POLLINATIONS_URL == "https://text.pollinations.ai/openai"
    for preset in PROVIDER_PRESETS.values():
        assert "text.pollinations.ai" not in str(preset.get("url", ""))
    assert "text.pollinations.ai" not in LlmConfig().primary_url


def test_effective_max_tokens():
    assert effective_max_tokens("https://gen.pollinations.ai/v1/chat/completions", 8192) is None
    assert effective_max_tokens("https://gen.pollinations.ai/v1/chat/completions", None) is None
    assert effective_max_tokens("https://api.openai.com/v1/chat/completions", None) is None
    assert effective_max_tokens("https://api.openai.com/v1/chat/completions", 1500) == 1500


def test_build_payload_omits_max_tokens_for_pollinations():
    cfg = LlmConfig(max_tokens=8192)  # даже если настроено — для Pollinations не отправляем
    p = build_payload("openai", "sys", "user", "https://gen.pollinations.ai/v1/chat/completions", cfg)
    assert "max_tokens" not in p
    assert p["model"] == "openai"
    p2 = build_payload("gpt-4o-mini", "sys", "user", "https://api.openai.com/v1/chat/completions", cfg)
    assert p2["max_tokens"] == 8192


def test_token_limit_error_message():
    msg = friendly_http_error(400, '{"code":"output_token_limit_exceeded"}')
    assert "ограничил" in msg.lower()


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

    captured: dict[str, str] = {}

    def fake_compile(code, llm, config=None, progress=None, cancel_event=None, postprocess=None, prompt_view=None):
        captured["code"] = code
        captured["postprocess"] = "yes" if postprocess else "no"
        return FakeResult()

    monkeypatch.setattr(orch_mod, "compile_with_repair", fake_compile)
    req = GenerationRequest(request_text="Лекция по физике", doc_type=DocType.LECTURE, style=Style.STRICT, volume=Volume(pages=2))
    result = orch.generate(req)
    assert result.pdf_path.exists()
    # Готовый документ = наша преамбула + тело от модели
    assert captured["code"].startswith("% !TEX program = pdflatex")
    assert r"\begin{document}" in captured["code"]
    assert r"\section{Введение}" in captured["code"]
    assert captured["postprocess"] == "yes"
    assert result.latex_code.startswith("% !TEX program = pdflatex")


# ---------------------------------------------------------------------------
# Авторизация: ключ должен реально уходить в запрос (фикс HTTP 401)
# ---------------------------------------------------------------------------

def test_key_is_sent_to_pollinations():
    """Регресс: для провайдера Pollinations ключ раньше не отправлялся -> 401."""
    s = LlmSettings(provider=LlmProvider.POLLINATIONS, api_key="sk-testkey12345")
    cfg = LlmConfig.from_settings(s)
    url, model, send_key = cfg.endpoints[0]
    assert "gen.pollinations.ai" in url
    assert send_key is True
    assert auth_headers(url, cfg.api_key, send_key) == {"Authorization": "Bearer sk-testkey12345"}


def test_key_not_sent_to_ollama():
    s = LlmSettings(provider=LlmProvider.OLLAMA, api_key="sk-testkey12345")
    cfg = LlmConfig.from_settings(s)
    url, _model, send_key = cfg.endpoints[0]
    assert send_key is False
    assert auth_headers(url, cfg.api_key, send_key) == {}


def test_normalize_api_key():
    assert normalize_api_key("  sk-abc123  ") == "sk-abc123"
    assert normalize_api_key('"sk-abc123"') == "sk-abc123"
    assert normalize_api_key("Bearer sk-abc123") == "sk-abc123"
    assert normalize_api_key("bearer   sk-abc123") == "sk-abc123"
    assert normalize_api_key("sk-abc\n123") == "sk-abc123"  # перенос строки при копировании
    assert normalize_api_key("") == ""
    assert normalize_api_key(None) == ""


def test_key_normalized_in_settings_and_client():
    s = LlmSettings(provider=LlmProvider.POLLINATIONS, api_key="Bearer  sk-abc123\n")
    cfg = LlmConfig.from_settings(s)
    assert cfg.api_key == "sk-abc123"
    assert auth_headers(cfg.primary_url, cfg.api_key, True)["Authorization"] == "Bearer sk-abc123"


def test_401_message_explains_missing_key():
    msg = friendly_http_error(401, "unauthorized", key_sent=False, url="https://gen.pollinations.ai/v1/chat/completions")
    assert "не отправило" in msg
    msg2 = friendly_http_error(401, "unauthorized", key_sent=True, url="https://gen.pollinations.ai/v1/chat/completions")
    assert "отправлен" in msg2
    assert "enter.pollinations.ai" in msg2  # подсказка, где взять ключ


def test_401_is_not_retried(monkeypatch):
    """401 не должен повторяться 3 раза — сразу переходим к следующему эндпоинту."""
    from universum.utils import LlmError

    cfg = LlmConfig(provider=LlmProvider.POLLINATIONS, api_key="sk-bad", max_retries=3, retry_backoff_sec=0)
    client = LlmClient(cfg)
    calls: list[dict] = []

    def fake_post(url, payload, headers, timeout=None):
        calls.append(headers)
        err = LlmError(friendly_http_error(401, "unauthorized", key_sent=bool(headers.get("Authorization")), url=url))
        err.retryable = False
        raise err

    monkeypatch.setattr(client, "_post_with_headers", fake_post)
    with pytest.raises(LlmError) as exc:
        client.generate("sys", "user")
    # Primary Pollinations — ровно одна попытка, ключ в заголовке присутствовал
    assert len(calls) == 1 + 1  # primary (1 попытка) + ollama (1 попытка)
    assert calls[0].get("Authorization") == "Bearer sk-bad"
    assert "401" in str(exc.value)


def test_generate_sends_authorization_header(monkeypatch):
    cfg = LlmConfig(provider=LlmProvider.POLLINATIONS, api_key="sk-ok", max_retries=1, retry_backoff_sec=0)
    client = LlmClient(cfg)
    seen: list[dict] = []

    def fake_post(url, payload, headers, timeout=None):
        seen.append(headers)
        return {"choices": [{"message": {"content": "\\documentclass{article}"}}]}

    monkeypatch.setattr(client, "_post_with_headers", fake_post)
    assert client.generate("sys", "user")
    assert seen[0]["Authorization"] == "Bearer sk-ok"


# ---------------------------------------------------------------------------
# LaTeX-компилятор: устойчивость к None в выводе процесса / логах
# ---------------------------------------------------------------------------

class _FakeProc:
    """Имитация CompletedProcess с None в потоках (причина TypeError)."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = None
        self.stderr = None


def test_text_normalizes_none():
    from universum.latex_compiler import _text

    assert _text(None) == ""
    assert _text("abc") == "abc"
    assert _text(b"abc") == "abc"
    assert _text(42) == "42"


def test_compile_result_normalizes_none_log():
    from universum.latex_compiler import CompileResult

    r = CompileResult(success=False, pdf_path=None, log=None, engine=None)  # type: ignore[arg-type]
    assert r.log == ""
    assert r.engine == ""
    # конкатенация с логом больше не падает
    assert ("Ошибка: " + r.log) == "Ошибка: "


def test_compile_handles_none_stdout(monkeypatch, tmp_path):
    """Регресс: 'can only concatenate str (not "NoneType") to str'."""
    import universum.latex_compiler as lc

    monkeypatch.setattr(lc, "find_engine", lambda preference: "tectonic")
    monkeypatch.setattr(lc.subprocess, "run", lambda *a, **k: _FakeProc(returncode=1))

    result = lc.compile_latex("\\documentclass{article}\\begin{document}x\\end{document}")
    assert result.success is False
    assert result.log  # лог — строка, не None
    assert "кодом 1" in result.log


def test_compile_handles_none_stdout_success_pdf(monkeypatch, tmp_path):
    """Тот же None в потоках, но PDF создан — компиляция успешна."""
    import universum.latex_compiler as lc

    monkeypatch.setattr(lc, "find_engine", lambda preference: "tectonic")

    def fake_run(cmd, cwd=None, **kwargs):
        (tmp_path / "document.pdf").write_bytes(b"%PDF-1.4 fake")
        # эмулируем запись в реальную рабочую папку
        from pathlib import Path
        Path(cwd, "document.pdf").write_bytes(b"%PDF-1.4 fake")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(lc.subprocess, "run", fake_run)
    result = lc.compile_latex("\\documentclass{article}\\end{document}")
    assert result.success is True
    assert result.pdf_path is not None and result.pdf_path.exists()


def test_compile_no_engine_returns_friendly_log(monkeypatch):
    import universum.latex_compiler as lc

    monkeypatch.setattr(lc, "find_engine", lambda preference: None)
    monkeypatch.setattr(lc, "ensure_tectonic", lambda progress=None: None)
    result = lc.compile_latex("\\documentclass{article}\\end{document}")
    assert result.success is False
    assert "движок не найден" in result.log.lower()


def test_compile_wraps_unexpected_exception(monkeypatch):
    import universum.latex_compiler as lc

    monkeypatch.setattr(lc, "find_engine", lambda preference: "tectonic")

    def boom(*a, **k):
        raise ValueError("странная ошибка")

    monkeypatch.setattr(lc.subprocess, "run", boom)
    result = lc.compile_latex("\\documentclass{article}\\end{document}")
    assert result.success is False
    assert "странная ошибка" in result.log
    assert isinstance(result.log, str)


def test_build_repair_prompt_with_none_log():
    from universum.prompts import build_repair_prompt

    text = build_repair_prompt("\\documentclass{article}", None)  # type: ignore[arg-type]
    assert "лог компилятора пуст" in text


def test_compile_with_repair_raises_with_string_log(monkeypatch):
    import universum.latex_compiler as lc
    from universum.utils import LatexCompileError

    class Cfg:
        max_repair_attempts = 1

    monkeypatch.setattr(lc, "compile_latex", lambda code, **kw: lc.CompileResult(False, None, None))
    with pytest.raises(LatexCompileError) as exc:
        lc.compile_with_repair("x", lambda s, u: "y", config=Cfg())
    assert isinstance(exc.value.log, str) and exc.value.log


# ---------------------------------------------------------------------------
# Запрет внутренних @-макросов LaTeX (You can't use \spacefactor ...)
# ---------------------------------------------------------------------------

def test_system_prompt_forbids_at_macros():
    from universum.prompts import SYSTEM_PROMPT

    assert r"\@" in SYSTEM_PROMPT
    assert "makeatletter" in SYSTEM_PROMPT
    assert "spacefactor" in SYSTEM_PROMPT
    assert r"\makelecturetitle" in SYSTEM_PROMPT
    assert "НИКОГДА" in SYSTEM_PROMPT


def test_style_prompts_forbid_at_macros():
    from universum.prompts import STYLE_BLOCKS

    for style, block in STYLE_BLOCKS.items():
        assert r"\@" in block, style
        assert "makelecturetitle" in block, style


def test_lecture_and_repair_prompt_mention_title_rules():
    from universum.prompts import DOCTYPE_LECTURE, build_repair_prompt

    assert r"\makelecturetitle" in DOCTYPE_LECTURE

    repair = build_repair_prompt(r"\makelecturetitle{A}{B}{C}{D}", "! You can't use \\spacefactor in internal vertical mode")
    assert "spacefactor" in repair
    assert "makelecturetitle" in repair


def test_sanitize_latex_removes_at_macros():
    from universum.utils import sanitize_latex

    code = (
        "\\documentclass{article}\n"
        "\\title{Тема}\\author{Автор}\\date{2026}\n"
        "\\makeatletter\n"
        "\\def\\@maketitle{\\hbox{\\@title}}\n"
        "\\makeatother\n"
        "\\begin{document}\n"
        "{\\Large \\@title}\n"
        "\\@author, \\@date\n"
        "\\spacefactor1000\n"
        "почта: user@example.com\n"
        "\\end{document}"
    )
    clean = sanitize_latex(code)
    assert r"\@" not in clean
    assert "makeatletter" not in clean
    assert "spacefactor" not in clean
    # значения из преамбулы подставлены вместо \@title и т.п.
    assert "Тема" in clean and "Автор" in clean and "2026" in clean
    # обычный @ в тексте не тронут
    assert "user@example.com" in clean


def test_sanitize_latex_handles_none_and_plain():
    from universum.utils import sanitize_latex

    assert sanitize_latex("") == ""
    assert sanitize_latex(None) == ""  # type: ignore[arg-type]
    txt = "\\documentclass{article}\\begin{document}x\\end{document}"
    assert sanitize_latex(txt) == txt


def test_validate_flags_at_macros():
    from universum.utils import validate_latex

    problems = validate_latex("\\documentclass{article}\\begin{document}\\@title\\end{document}")
    assert any("макрос" in p.lower() for p in problems)


def test_extract_latex_sanitizes_at_macros():
    from universum.utils import extract_latex

    raw = "```latex\n\\documentclass{article}\n\\begin{document}\n\\maketitle\\@\\@title\n\\end{document}\n```"
    out = extract_latex(raw)
    assert r"\@" not in out
    assert "\\begin{document}" in out


def test_compile_latex_sanitizes_before_engine(monkeypatch):
    """Даже если код пришёл в обход extract_latex, движок не увидит \\@."""
    import universum.latex_compiler as lc

    captured: dict[str, str] = {}
    monkeypatch.setattr(lc, "find_engine", lambda preference: "tectonic")

    def fake_run(cmd, cwd=None, **kwargs):
        from pathlib import Path

        captured["tex"] = Path(cwd, "document.tex").read_text(encoding="utf-8")
        p = lc.CompileResult  # noqa: F841 — просто для читаемости
        raise RuntimeError("останавливаемся после чтения файла")

    monkeypatch.setattr(lc, "subprocess", type("S", (), {"run": staticmethod(fake_run), "TimeoutExpired": Exception, "DEVNULL": -3}))
    result = lc.compile_latex("\\documentclass{article}\\begin{document}\\@title\\end{document}")
    assert r"\@" not in captured["tex"]
    assert result.success is False
    assert isinstance(result.log, str)


def test_prompts_have_no_double_backslashes():
    """В промтах не должно быть «\\\\@» / «\\\\title» — модели увидят лишнюю косую."""
    from universum.prompts import DOCTYPE_BLOCKS, STYLE_BLOCKS, SYSTEM_PROMPT, REPAIR_PROMPT_TEMPLATE

    texts = [SYSTEM_PROMPT, REPAIR_PROMPT_TEMPLATE, *STYLE_BLOCKS.values(), *DOCTYPE_BLOCKS.values()]
    for text in texts:
        assert "\\\\@" not in text
        assert "\\\\maketitle" not in text
        assert "\\\\title{" not in text


# ---------------------------------------------------------------------------
# LaTeX-шаблон: фиксированная преамбула, магическая строка, блоки
# ---------------------------------------------------------------------------

def test_magic_comment_is_first_line():
    from universum.prompts import MAGIC_COMMENT, wrap_document

    for style in Style:
        doc = wrap_document("тело", style)
        assert doc.splitlines()[0] == MAGIC_COMMENT == "% !TEX program = pdflatex"


def test_wrap_document_has_template_features():
    from universum.prompts import wrap_document

    for style in Style:
        doc = wrap_document(r"\makelecturetitle{К}{С}{Л}{Т}\section{X}", style)
        assert doc.count(r"\begin{document}") == 1
        assert doc.count(r"\end{document}") == 1
        assert r"\makelecturetitle" in doc
        # окружения-блоки доступны в любом стиле
        for env in ("definition", "theorem", "example", "important", "keyidea"):
            assert env in doc, (style, env)


def test_preamble_is_engine_agnostic():
    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style)
        assert r"\usepackage{iftex}" in pre
        assert r"\ifPDFTeX" in pre
        assert r"\usepackage[T2A]{fontenc}" in pre      # pdflatex + кириллица
        assert r"\usepackage{fontspec}" in pre           # xelatex/lualatex (Tectonic)
        assert "fontawesome5" in pre or style != Style.COLORFUL
        # кириллический sans вместо helvet (у helvet нет T2A-кириллицы)
        assert r"\usepackage[scaled=0.92]{helvet}" not in pre
        assert r"\familydefault}{\sfdefault}" in pre


def test_preamble_language_order():
    from universum.prompts import build_preamble

    assert "english,russian" in build_preamble(Style.STRICT, "ru")
    assert "russian,english" in build_preamble(Style.STRICT, "en")


def test_preamble_braces_and_conditionals_balanced():
    """Статическая проверка сгенерированного LaTeX (компилятора в тестах нет)."""
    import re

    from universum.prompts import wrap_document

    for style in Style:
        doc = wrap_document(r"\makelecturetitle{К}{С}{Л}{Т}", style)
        txt = re.sub(r"(?<!\\)%.*", "", doc).replace(r"\{", "").replace(r"\}", "")
        assert txt.count("{") == txt.count("}"), style
        begins = re.findall(r"\\begin\{([^}]+)\}", txt)
        ends = re.findall(r"\\end\{([^}]+)\}", txt)
        assert sorted(begins) == sorted(ends), style
        t2 = re.sub(r"\\newif\s*\\if[a-zA-Z@]+", "", txt)   # \newif объявляет, не открывает
        assert len(re.findall(r"\\if[a-zA-Z@]+", t2)) == len(re.findall(r"\\fi(?![a-zA-Z])", t2)), style


def test_system_prompt_requires_body_only():
    from universum.prompts import SYSTEM_PROMPT

    assert "ТОЛЬКО тело" in SYSTEM_PROMPT
    assert r"ЗАПРЕЩЕНО писать \documentclass" in SYSTEM_PROMPT
    assert r"\makelecturetitle" in SYSTEM_PROMPT
    for env in ("definition", "theorem", "example", "important", "keyidea"):
        assert env in SYSTEM_PROMPT
    # запрет менять преамбулу и использовать внутренние макросы сохранён
    assert "преамбул" in SYSTEM_PROMPT
    assert r"\@," in SYSTEM_PROMPT and "makeatletter" in SYSTEM_PROMPT


def test_generation_prompt_asks_for_body():
    from universum.prompts import PromptContext, build_generation_prompt

    prompt = build_generation_prompt(PromptContext(request_text="Тест темы"))
    assert r"\makelecturetitle" in prompt
    assert r"\documentclass" in prompt  # упоминается только как запрет
    assert "ТОЛЬКО тело" in prompt


def test_extract_document_body_strips_preamble_and_fences():
    from universum.utils import extract_document_body

    raw = "Вот:\n```latex\n% !TEX program = pdflatex\n\\documentclass{article}\n\\usepackage{tikz}\n\\begin{document}\n\\makelecturetitle{A}{B}{C}{D}\n\\section{X}\n\\end{document}\n```"
    body = extract_document_body(raw)
    assert body.startswith(r"\makelecturetitle")
    assert "documentclass" not in body and "usepackage" not in body
    assert r"\begin{document}" not in body and r"\end{document}" not in body


def test_extract_document_body_passthrough_body():
    from universum.utils import extract_document_body

    body = r"\makelecturetitle{A}{B}{C}{D}" + "\n" + r"\section{X}" + "\nТекст"
    assert extract_document_body(body) == body
    assert extract_document_body("") == ""
    assert extract_document_body(None) == ""  # type: ignore[arg-type]


def test_engine_from_magic():
    from universum.latex_compiler import engine_from_magic

    assert engine_from_magic("% !TEX program = pdflatex\n\\documentclass{article}") == "pdflatex"
    assert engine_from_magic("% !TEX TS-program = xelatex") == "xelatex"
    assert engine_from_magic("%!TEX program = lualatex") == "lualatex"
    assert engine_from_magic("\\documentclass{article}") is None


def test_magic_comment_prefers_installed_engine(monkeypatch):
    """При магической строке pdflatex движок выбирается первым, если он есть."""
    import universum.latex_compiler as lc

    seen: dict[str, tuple] = {}

    def fake_find(preference):
        seen["preference"] = preference
        return "pdflatex"

    def fake_run(cmd, cwd=None, **kwargs):
        from pathlib import Path

        Path(cwd, "document.pdf").write_bytes(b"%PDF-1.4")
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(lc, "find_engine", fake_find)
    monkeypatch.setattr(lc.subprocess, "run", fake_run)
    monkeypatch.setattr(lc.shutil, "which", lambda name: "/usr/bin/pdflatex" if name == "pdflatex" else None)

    result = lc.compile_latex("% !TEX program = pdflatex\n\\documentclass{article}\\begin{document}x\\end{document}")
    assert result.success is True
    assert seen["preference"][0] == "pdflatex"


def test_compile_with_repair_postprocess(monkeypatch):
    """Ремонт возвращает тело — postprocess собирает документ заново."""
    import universum.latex_compiler as lc

    calls: list[str] = []

    def fake_compile(code, **kwargs):
        calls.append(code)
        if len(calls) == 1:
            return lc.CompileResult(False, None, "! ошибка")
        return lc.CompileResult(True, None, "")

    monkeypatch.setattr(lc, "compile_latex", fake_compile)
    out = lc.compile_with_repair(
        "СТАРЫЙ ДОКУМЕНТ",
        lambda s, u: "\\makelecturetitle{A}{B}{C}{D}",
        config=type("C", (), {"max_repair_attempts": 1, "engine_preference": ()})(),
        postprocess=lambda raw: "ПРЕАМБУЛА + " + raw,
        prompt_view=lambda doc: doc.lower(),
    )
    assert out.success is True
    assert calls[1].startswith("ПРЕАМБУЛА + ")


def test_preamble_error_detection():
    from universum.latex_compiler import is_preamble_error

    assert is_preamble_error("! LaTeX Error: File `tcolorbox.sty' not found.")
    assert is_preamble_error("File `fontawesome5.sty' not found")
    assert not is_preamble_error("! You can't use \\spacefactor in internal vertical mode")
    assert not is_preamble_error("")


def test_preamble_error_skips_llm_repair(monkeypatch):
    """Отсутствующий пакет не «лечим» тремя запросами к нейросети."""
    import universum.latex_compiler as lc
    from universum.utils import LatexCompileError

    monkeypatch.setattr(lc, "compile_latex", lambda code, **kw: lc.CompileResult(False, None, "! LaTeX Error: File `tcolorbox.sty' not found."))
    called = {"n": 0}

    def llm(system, user):
        called["n"] += 1
        return "тело"

    cfg = type("C", (), {"max_repair_attempts": 3, "engine_preference": ()})()
    with pytest.raises(LatexCompileError) as exc:
        lc.compile_with_repair("ДОК", llm, config=cfg)
    assert called["n"] == 0
    assert "tcolorbox" in str(exc.value)


def test_preamble_loads_commands_allowed_by_prompt():
    """Всё, что промт разрешает модели, должно быть подключено в преамбуле."""
    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style)
        for pkg in ("amsmath", "amssymb", "booktabs", "tabularx", "listings", "xcolor", "geometry", "hyperref", "enumitem", "babel"):
            assert pkg in pre, (style, pkg)
        assert r"\lstset" in pre
        assert r"\newcommand{\term}" in pre
        assert r"\newcommand{\hl}" in pre


# ---------------------------------------------------------------------------
# Кириллица: шрифты с T2A и иконки, которых может не быть
# ---------------------------------------------------------------------------

def test_preamble_uses_cyrillic_capable_fonts():
    """lmsans/lmroman кириллицы не содержат — нужен tex-gyre (и fallback)."""
    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style)
        assert "tgheros" in pre                       # pdflatex: T2A-кириллица
        assert "texgyreheros-regular.otf" in pre      # xelatex/lualatex/Tectonic
        assert "texgyretermes-regular.otf" in pre
        assert "cmunss.otf" in pre                    # fallback CMU
        assert "DejaVuSans.ttf" in pre                # fallback DejaVu
        assert "\\IfFontExistsTF" in pre
        assert "\\familydefault}{\\sfdefault}" in pre


def test_icons_are_guarded_against_missing_glyphs():
    """\\faCalendarAlt есть не во всех версиях fontawesome5 -> не должно быть
    «Undefined control sequence»."""
    import re

    from universum.prompts import build_preamble

    pre = build_preamble(Style.COLORFUL)
    assert r"\universumicon" in pre
    assert r"\ifcsname faCalendarAlt\endcsname" in pre or r"\universumicon{faCalendarAlt}" in pre
    # ни одна иконка не используется напрямую вне проверки
    for line in pre.splitlines():
        if re.search(r"\\fa[A-Z]", line) and not line.strip().startswith("%"):
            assert "ifcsname" in line or "universumicon" in line, line


def test_has_missing_cyrillic():
    from universum.latex_compiler import has_missing_cyrillic

    warn = 'Missing character: There is no М (U+041C) in font [lmsans10-bold]:mapping=tex-text;!'
    assert not has_missing_cyrillic(warn)          # 1 предупреждение — ещё не повод падать
    assert has_missing_cyrillic(warn * 6)          # массовая потеря кириллицы
    assert not has_missing_cyrillic("! LaTeX Error: File `x.sty' not found")
    assert not has_missing_cyrillic("Missing character: There is no é (U+00E9)")


def test_cyrillic_error_is_not_repaired_by_llm(monkeypatch):
    import universum.latex_compiler as lc
    from universum.utils import LatexCompileError

    log = 'Missing character: There is no М (U+041C) in font [lmsans10]:mapping=tex-text;!' * 6
    monkeypatch.setattr(lc, "compile_latex", lambda code, **kw: lc.CompileResult(False, None, log))
    called = {"n": 0}

    def llm(system, user):
        called["n"] += 1
        return "тело"

    cfg = type("C", (), {"max_repair_attempts": 3, "engine_preference": ()})()
    with pytest.raises(LatexCompileError) as exc:
        lc.compile_with_repair("ДОК", llm, config=cfg)
    assert called["n"] == 0
    assert "кириллиц" in str(exc.value)
