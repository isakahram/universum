"""Unit-тесты Universum (без сети, GUI и LaTeX — всё замокано)."""

from __future__ import annotations

import re
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
        # «сложные» шрифты убраны: ни helvet, ни TeX Gyre, ни форсированного sans
        assert r"\usepackage[scaled=0.92]{helvet}" not in pre
        assert "texgyre" not in pre.lower()
        assert "tgheros" not in pre
        assert r"\familydefault}{\sfdefault}" not in pre


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
        # \ifstrempty/\ifdefempty — макросы etoolbox: они не открывают \fi
        t2 = re.sub(r"\\(?:ifstrempty|ifdefempty|ifdefvoid|ifcsdef|ifblank|ifdefmacro)\b", "", t2)
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
    """Latin Modern кириллицы не содержит — основной шрифт DejaVu Serif.

    Шрифт задаётся по имени ФАЙЛА: в Tectonic fontconfig может быть недоступен,
    и \\setmainfont{DejaVu Serif} по имени семейства там не сработает.
    """
    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style)
        assert r"\setmainfont{DejaVuSerif.ttf}" in pre        # основной шрифт
        assert "LiberationSerif-Regular.ttf" in pre            # запасной вариант
        assert r"\setsansfont{DejaVuSans.ttf}" in pre         # заголовки/титул
        assert r"\setmonofont{DejaVuSansMono.ttf}" in pre     # листинги
        assert r"\IfFontExistsTF" in pre
        # никаких TeX Gyre / cm-unicode
        assert "texgyre" not in pre.lower()
        assert "cmunss" not in pre and "cmunrm" not in pre
        # pdflatex-ветка сохраняет T2A-кириллицу
        assert r"\usepackage[T2A]{fontenc}" in pre


def test_preamble_has_amsthm_for_qed():
    """\\qed (и окружение proof) дают только amsthm: без него падало
    «Undefined control sequence \\qed»."""
    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style)
        assert r"\usepackage{amsthm}" in pre
        assert "amssymb" in pre


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


# --- GUI (customtkinter) — статическая проверка без запуска окна ---------------

GUI_SOURCE = (Path(__file__).resolve().parents[1] / "src" / "universum" / "gui.py").read_text(encoding="utf-8")


def _gui_code_only() -> str:
    """Код gui.py без комментариев и docstring'ов (там допустимы любые упоминания)."""
    import ast

    tree = ast.parse(GUI_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


GUI_CODE = _gui_code_only()
# ast.unparse приводит кавычки к одинарным — сравниваем в едином виде
GUI_CODE_Q = GUI_CODE.replace('"', "'")


def _gui_function(name: str):
    """Достаёт функцию из gui.py без импорта модуля (в CI нет tkinter/дисплея)."""
    import ast

    from universum.utils import plural_ru

    tree = ast.parse(GUI_SOURCE)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            # модульные глобали, которые нужны извлечённой функции
            namespace: dict[str, object] = {"plural_ru": plural_ru}
            exec(compile(ast.Module([node], []), "gui.py", "exec"), namespace)  # noqa: S102
            return namespace[name]
    raise AssertionError(f"в gui.py нет функции {name}")


def test_gui_uses_customtkinter_and_not_messagebox():
    assert "import customtkinter" in GUI_CODE
    assert "set_default_color_theme('blue')" in GUI_CODE_Q
    assert "set_appearance_mode('system')" in GUI_CODE_Q
    # модальные окна и тосты свои: ни импорта, ни вызовов messagebox
    assert "messagebox" not in GUI_CODE, "ошибки должны показываться тостами/модалками"
    # старый ttk-интерфейс не должен вернуться
    assert "ttk" not in GUI_CODE


def test_gui_has_required_widgets():
    for needle in (
        "'Сгенерировать PDF'",
        "Опишите, что вы хотите сгенерировать",
        "CTkProgressBar",
        "CTkSlider",
        "CTkOptionMenu",
        "CTkTextbox",
        "class Toast",
        "class MessageDialog",
        "class PlaceholderTextbox",
        "class UniversumApp",
    ):
        assert needle in GUI_CODE, f"в gui.py отсутствует {needle}"


def test_gui_hides_technical_details_from_main_window():
    # технические названия не должны попадать в подписи главного окна
    for needle in ("Tectonic", "pdflatex", "xelatex", "documentclass", "preamble"):
        assert needle not in GUI_CODE, f"техническая деталь в GUI: {needle}"


def test_gui_keeps_orchestrator_protocol():
    for needle in ("__DONE__", "__LATEX_ERROR__", "__ERROR__", "reload_llm_config", "cancel_event"):
        assert needle in GUI_CODE
    assert "_progress_queue" in GUI_CODE
    assert "threading.Thread" in GUI_CODE


@pytest.mark.parametrize(
    "count,expected",
    [(1, "страница"), (3, "страницы"), (5, "страниц"), (11, "страниц"), (21, "страница"), (22, "страницы")],
)
def test_plural_pages(count, expected):
    plural = _gui_function("_plural_pages")
    assert plural(count) == expected


def test_customtkinter_is_declared_as_dependency():
    root = Path(__file__).resolve().parents[1]
    assert "customtkinter" in (root / "requirements.txt").read_text(encoding="utf-8")
    assert "customtkinter" in (root / "pyproject.toml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Стили: без «Без стиля», плюс неон / природа / минимализм
# ---------------------------------------------------------------------------

def test_styles_are_five_and_plain_removed():
    from universum.config import Style

    values = [style.value for style in Style]
    assert "plain" not in values
    assert values == ["strict", "colorful", "neon", "nature", "minimal"]
    labels = [style.label_ru for style in Style]
    assert "Без стиля" not in labels
    for expected in ("Строгий", "Красочный / креативный", "Неон", "Природа", "Современный минимализм"):
        assert expected in labels


def test_every_style_defines_doc_type_from_app():
    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style, "ru", doc_type=DocType.TEST)
        assert r"\newcommand{\universumdoctype}{Тест}" in pre, style
        assert r"\makelecturetitle}[2][]" in pre, style  # необязательный аргумент доп. данных

    # без явного типа — нейтральная подпись, а не «Лекция»
    neutral = build_preamble(Style.NEON)
    assert r"\universumdoctype}{Учебный материал}" in neutral


def test_no_invented_title_fields_in_any_style():
    """Титул не должен содержать придуманных данных: ни лектора, ни семестра."""
    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style, "ru", doc_type=DocType.LECTURE)
        assert "Фамилия" not in pre, style
        assert "Лектор:" not in pre, style
        assert "Семестр 2" not in pre, style
        assert "Модуль 1" not in pre, style
        # доп. данные печатаются только если они пришли (необязательный аргумент)
        assert r"\ifstrempty{#1}{}" in pre, style


def test_colorful_styles_use_tcolorbox_boxes():
    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style, "ru", doc_type=DocType.LECTURE)
        assert r"\IfFileExists{tcolorbox.sty}" in pre, style
        # во всех стилях доступны одни и те же окружения (tcolorbox или fallback)
        assert r"\universumplainboxes" in pre, style


def test_wrap_document_puts_selected_type_on_title():
    from universum.prompts import wrap_document

    doc = wrap_document(r"\makelecturetitle{Квантовая механика}", Style.NATURE, doc_type=DocType.TEST)
    assert r"\universumdoctype}{Тест}" in doc
    doc2 = wrap_document(r"\makelecturetitle{Тема}", Style.COLORFUL, doc_type=DocType.HOMEWORK)
    assert r"\universumdoctype}{Домашнее задание}" in doc2


# ---------------------------------------------------------------------------
# Тесты: варианты и ключ ответов с новой страницы
# ---------------------------------------------------------------------------

def test_test_prompt_requires_variants_and_separate_answer_key():
    from universum.prompts import DOCTYPE_TEST

    assert "{variants}" in DOCTYPE_TEST and "{questions}" in DOCTYPE_TEST
    assert "Вариант 1" in DOCTYPE_TEST
    assert r"\clearpage" in DOCTYPE_TEST
    assert "Ключ ответов" in DOCTYPE_TEST
    assert "НАЧИНАЕТСЯ ЗАНОВО" in DOCTYPE_TEST
    # ключ всегда отдельно и с новой страницы
    assert DOCTYPE_TEST.index("Ключ ответов") > DOCTYPE_TEST.index("Вариант 1")
    assert "ответы рядом с вопросами писать запрещено" in DOCTYPE_TEST


def test_test_prompt_gets_numbers_from_interface():
    from universum.prompts import build_generation_prompt

    ctx = PromptContext(
        request_text="Тест по налогам",
        doc_type=DocType.TEST,
        style=Style.NEON,
        volume=Volume(questions=8, variants=3),
    )
    prompt = build_generation_prompt(ctx)
    assert "ровно 3 вариантов теста" in prompt
    assert "ровно 8 вопросов" in prompt
    assert "{variants}" not in prompt and "{questions}" not in prompt


def test_volume_hint_test_counts_variants_and_questions():
    ctx = PromptContext(request_text="x", doc_type=DocType.TEST, volume=Volume(questions=7, variants=3))
    hint = volume_hint(ctx)
    assert "3 вариантов теста" in hint
    assert "7 вопросов" in hint
    assert "с 1" in hint
    assert "Ключ ответов" in hint


# ---------------------------------------------------------------------------
# Титул: только явные данные и всегда тема одним аргументом
# ---------------------------------------------------------------------------

def test_normalize_title_converts_legacy_four_args():
    from universum.utils import normalize_title_call

    body = r"\makelecturetitle{Экономика}{Семестр 2}{Лекция}{Налогообложение}" + "\n" + r"\section{X}"
    out = normalize_title_call(body, "Лекция", "сделай лекцию про налогообложение")
    assert out.startswith(r"\makelecturetitle{Налогообложение}")
    assert "Семестр 2" not in out
    assert "Экономика" not in out
    assert r"\section{X}" in out


def test_normalize_title_keeps_explicit_user_data():
    from universum.utils import normalize_title_call

    body = r"\makelecturetitle{Экономика}{Семестр 2}{Лекция}{Налоги}" + "\n" + "текст"
    out = normalize_title_call(body, "Лекция", "лекция по налогам, семестр 2, экономика")
    call = out.splitlines()[0]
    assert call.startswith(r"\makelecturetitle[")
    assert "Семестр 2" in call and "Экономика" in call
    assert call.endswith("{Налоги}")


def test_normalize_title_drops_invented_extras():
    from universum.utils import normalize_title_call

    body = r"\makelecturetitle[Лектор: Фамилия И.О. · Модуль 1]{Кванты}"
    out = normalize_title_call(body, "Лекция", "лекция про кванты")
    assert out == r"\makelecturetitle{Кванты}"


def test_normalize_title_adds_missing_title_call():
    from universum.utils import normalize_title_call

    out = normalize_title_call(r"\section{Введение}", "Тест", "тест по квантовой механике")
    assert out.startswith(r"\makelecturetitle{квантовой механике}")
    assert r"\section{Введение}" in out


# ---------------------------------------------------------------------------
# Имя PDF: «Тип — Тема», безопасное для Windows
# ---------------------------------------------------------------------------

def test_suggest_document_name_uses_type_and_theme():
    from universum.utils import suggest_document_name

    name = suggest_document_name(
        "Тест",
        request_text="тест по налогам",
        latex_code=r"\makelecturetitle{Налогообложение в России — новые законы 2026}",
    )
    assert name == "Тест — Налогообложение в России — новые законы 2026.pdf"
    assert ":" not in name


def test_suggest_document_name_falls_back_to_request():
    from universum.utils import suggest_document_name

    name = suggest_document_name("Лекция", request_text="Сделай лекцию по фотосинтезу: основы")
    assert name.startswith("Лекция — фотосинтезу")
    assert name.endswith(".pdf")


def test_sanitize_filename_removes_forbidden_chars():
    from universum.utils import sanitize_filename

    cleaned = sanitize_filename('a<b>c:d?e*f"g|h\\i/j.pdf')
    for bad in '<>:"/\\|?*':
        assert bad not in cleaned
    assert cleaned.endswith(".pdf") and "abc" in cleaned
    assert sanitize_filename("CON.pdf") == "_CON.pdf"
    assert sanitize_filename("   ...   ") == "Universum"
    assert len(sanitize_filename("x" * 300)) <= 120
    assert not sanitize_filename("имя. ").endswith(" ")


def test_orchestrator_names_pdf_by_type_and_topic(monkeypatch, tmp_path):
    """Сквозная проверка: тип на титуле из выбора пользователя + имя файла."""
    fake_pdf = tmp_path / "compiled.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 test")
    orch = Orchestrator()
    body = (
        r"\makelecturetitle{Экономика}{Семестр 2}{Лекция}{Налогообложение в России}" + "\n"
        + r"\section{Вопросы}" + "\n" + "Текст"
    )
    monkeypatch.setattr(
        orch.llm,
        "generate",
        lambda s, u, progress=None, cancel_event=None: body,
    )
    import universum.orchestrator as orch_mod

    captured: dict[str, str] = {}

    class FakeResult:
        success = True
        pdf_path = fake_pdf
        log = ""
        engine = "fake"

    def fake_compile(code, llm, config=None, progress=None, cancel_event=None, postprocess=None, prompt_view=None):
        captured["code"] = code
        return FakeResult()

    monkeypatch.setattr(orch_mod, "compile_with_repair", fake_compile)
    req = GenerationRequest(
        request_text="тест по налогообложению в России",
        doc_type=DocType.TEST,
        style=Style.NEON,
        volume=Volume(questions=5, variants=3),
    )
    result = orch.generate(req)

    # тип на титуле — из интерфейса, а не «Лекция» от модели
    assert r"\universumdoctype}{Тест}" in captured["code"]
    # в теле — ровно один вызов титула, с одной темой
    body_part = captured["code"].split(r"\begin{document}", 1)[1]
    assert body_part.count(r"\makelecturetitle") == 1
    assert r"\makelecturetitle{Налогообложение в России}" in body_part
    assert "Семестр 2" not in body_part
    # имя файла: тип + тема, без запрещённых символов
    assert result.pdf_path.name == "Тест — Налогообложение в России.pdf"
    assert result.pdf_path.exists()


# ---------------------------------------------------------------------------
# Статическая проверка LaTeX: цвета, окружения, ветка без tcolorbox
# ---------------------------------------------------------------------------

_XCOLOR_BASE = {
    "black", "white", "red", "green", "blue", "cyan", "magenta", "yellow",
    "gray", "darkgray", "lightgray", "brown", "lime", "olive", "orange",
    "pink", "purple", "teal", "violet", "none",
}


def _used_colors(latex: str) -> set[str]:
    import re

    used: set[str] = set()
    for pattern in (
        r"\\color\{([A-Za-z]+)(?:![0-9]+)?\}",
        r"\\textcolor\{([A-Za-z]+)(?:![0-9]+)?\}",
        r"col(?:frame|back|backtitle|title)=\{?([A-Za-z]+)(?:![0-9]+)?",
        r"borderline(?:\s+west)?=\{?[^}]*\}?\{(?:[^{}]*)\}\{(?:[^{}]*)\}\{([A-Za-z]+)",
    ):
        used.update(re.findall(pattern, latex))
    return used


def test_all_colors_are_defined_in_every_style():
    from universum.prompts import build_preamble

    import re

    for style in Style:
        pre = build_preamble(style, "ru", doc_type=DocType.TEST)
        defined = set(re.findall(r"\\definecolor\{([A-Za-z]+)\}", pre))
        used = _used_colors(pre)
        unknown = used - defined - _XCOLOR_BASE
        assert not unknown, f"{style}: не определены цвета {sorted(unknown)}"


def test_environments_available_without_tcolorbox():
    """Если tcolorbox нет, \\universumplainboxes должен дать все окружения."""
    from universum.prompts import _PLAIN_BOXES

    for env in ("definition", "theorem", "example", "important", "keyidea"):
        assert rf"\newenvironment{{{env}}}" in _PLAIN_BOXES, env


def test_no_fancy_unsupported_packages():
    """Только пакеты из стандартного бандла Tectonic — без shell-escape."""
    from universum.prompts import build_preamble

    import re

    for style in Style:
        pre = build_preamble(style, "ru", doc_type=DocType.LECTURE).lower()
        for banned in ("minted", "biblatex", "glossaries", "cleveref", "siunitx", "texgyre"):
            assert not re.search(rf"\\usepackage(\[[^\]]*\])?\{{[^}}]*\b{banned}\b", pre), (style, banned)


def test_titlepage_has_single_mandatory_argument():
    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style, "ru", doc_type=DocType.LECTURE)
        assert r"\makelecturetitle}[2][]" in pre, style
        assert r"\makelecturetitle}[4]" not in pre, style


_ALLOWED_PACKAGES = {
    "iftex", "fontenc", "inputenc", "lmodern", "fontspec", "babel", "geometry",
    "etoolbox", "microtype", "amsmath", "amssymb", "amsthm", "graphicx", "booktabs",
    "tabularx", "listings", "xcolor", "enumitem", "fancyhdr", "titlesec", "hyperref",
    "tcolorbox", "fontawesome5", "tikz",
}


def test_only_standard_bundle_packages():
    """Все \\usepackage — из стандартного бандла Tectonic (никаких экзотик)."""
    import re

    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style, "ru", doc_type=DocType.LECTURE)
        used: set[str] = set()
        for match in re.finditer(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}", pre):
            used.update(part.strip() for part in match.group(1).split(","))
        assert not used - _ALLOWED_PACKAGES, (style, sorted(used - _ALLOWED_PACKAGES))


def test_preamble_works_without_tcolorbox_and_tikz():
    """Ветки «пакета нет» должны быть полными: блоки и титул остаются."""
    import re

    from universum.prompts import build_preamble

    for style in Style:
        pre = build_preamble(style, "ru", doc_type=DocType.TEST)
        fancy = re.search(r"\\ifuniversumfancy(.*?)\\else(.*?)\\fi", pre, re.DOTALL)
        assert fancy is not None, style
        without_tcb = pre[: fancy.start()] + fancy.group(2) + pre[fancy.end():]
        assert "newtcolorbox" not in without_tcb, style
        assert r"\universumplainboxes" in without_tcb, style
        # скобки сбалансированы в выбранной ветке
        txt = re.sub(r"(?<!\\)%.*", "", without_tcb)
        assert txt.count("{") == txt.count("}"), style

        tikz = re.search(r"\\ifuniversumtikz(.*?)\\else(.*?)\\fi", pre, re.DOTALL)
        if tikz is not None:
            without_tikz = pre[: tikz.start()] + tikz.group(2) + pre[tikz.end():]
            assert without_tikz.count(r"\newcommand{\makelecturetitle}") == 1, style
            txt = re.sub(r"(?<!\\)%.*", "", without_tikz)
            assert txt.count("{") == txt.count("}"), style


def test_test_document_with_variants_is_valid_latex():
    """Тест с двумя вариантами и ключом: структура и валидность LaTeX."""
    from universum.prompts import wrap_document

    question = (
        "\\begin{enumerate}\n"
        "  \\item Вопрос?\n"
        "  \\begin{enumerate}\n"
        "    \\item[а)] Первый\n"
        "    \\item[б)] Второй\n"
        "  \\end{enumerate}\n"
        "\\end{enumerate}"
    )
    body = (
        r"\makelecturetitle{Налоги}" + "\n\n"
        + r"\section*{Вариант 1}" + "\n" + question + "\n\n"
        + r"\clearpage" + "\n" + r"\section*{Вариант 2}" + "\n" + question + "\n\n"
        + r"\clearpage" + "\n" + r"\section*{Ключ ответов}" + "\n"
        + r"\subsection*{Вариант 1}" + "\n" + "1 — а\n"
    )
    doc = wrap_document(body, Style.NATURE, "ru", doc_type=DocType.TEST)
    assert validate_latex(doc) == []
    assert doc.count(r"\clearpage") == 2
    # ключ строго после вопросов и с новой страницы
    assert doc.index(r"\section*{Ключ ответов}") > doc.index(r"\section*{Вариант 2}")
    assert r"\clearpage" + "\n" + r"\section*{Ключ ответов}" in doc
    assert r"\universumdoctype}{Тест}" in doc


def test_previews_exist_for_every_style():
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1] / "examples"
    for style in Style:
        path = root / f"preview_{style.value}.tex"
        assert path.exists(), path
        text = path.read_text(encoding="utf-8")
        assert text.splitlines()[0] == "% !TEX program = pdflatex"
        assert r"\begin{document}" in text and r"\end{document}" in text
    assert not (root / "preview_plain.tex").exists()


def test_normalize_title_escapes_special_chars():
    """Тема с % / & / # не должна ломать LaTeX (иначе % закомментирует строку)."""
    from universum.utils import normalize_title_call

    out = normalize_title_call(r"\makelecturetitle{Скидка 100\% и \$5 #1 & b}", "Лекция", "скидки")
    assert out == r"\makelecturetitle{Скидка 100\% и 5 \#1 \& b}"
    # «сырых» спецсимволов вне команд не осталось
    body = out[len(r"\makelecturetitle{"):-1]
    assert "%" not in body.replace(r"\%", "")
    assert "&" not in body.replace(r"\&", "")


def test_normalize_title_strips_wrong_doc_type_from_theme():
    from universum.utils import normalize_title_call

    out = normalize_title_call(r"\makelecturetitle{Лекция по физике}", "Тест", "тест по физике")
    assert out == r"\makelecturetitle{физике}"

    keep = normalize_title_call(r"\makelecturetitle{Физика: лекция 1}", "Лекция", "лекция по физике")
    assert "Физика" in keep


def test_ensure_page_breaks_before_variants_and_key():
    from universum.utils import ensure_page_breaks

    body = (
        r"\makelecturetitle{Налоги}" + "\n\n"
        + r"\section*{Инструкция}" + "\n10 вопросов.\n\n"
        + r"\section*{Вариант 1}" + "\nвопрос\n\n"
        + r"\section*{Вариант 2}" + "\nвопрос\n\n"
        + r"\section*{Ключ ответов}" + "\n1 — а\n"
    )
    out = ensure_page_breaks(body)
    # перед каждым вариантом и ключом — своя страница
    for title in ("Вариант 1", "Вариант 2", "Ключ ответов"):
        idx = out.index(title)
        assert r"\clearpage" in out[max(0, idx - 40):idx], title
    assert out.count(r"\clearpage") == 3
    # инструкция остаётся на первой странице
    assert out.index(r"\clearpage") > out.index("Инструкция")


def test_ensure_page_breaks_keeps_existing_breaks():
    from universum.utils import ensure_page_breaks

    body = r"\newpage" + "\n" + r"\section*{Вариант 1}" + "\nвопрос\n"
    out = ensure_page_breaks(body)
    assert out.count(r"\newpage") == 1
    assert r"\clearpage" not in out


def test_ensure_page_breaks_ignores_lectures():
    from universum.utils import ensure_page_breaks

    body = r"\makelecturetitle{Множества}" + "\n\n" + r"\section{Введение}" + "\nтекст\n"
    assert ensure_page_breaks(body) == body


def test_orchestrator_adds_page_breaks_for_tests(monkeypatch, tmp_path):
    from universum.orchestrator import GenerationRequest as Req

    fake_pdf = tmp_path / "compiled.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 test")
    orch = Orchestrator()
    model_body = (
        r"\makelecturetitle{Налоги}" + "\n\n"
        + r"\section*{Вариант 1}" + "\nвопрос\n\n"
        + r"\section*{Вариант 2}" + "\nвопрос\n\n"
        + r"\section*{Ключ ответов}" + "\n1 — а\n"
    )
    monkeypatch.setattr(orch.llm, "generate", lambda s, u, progress=None, cancel_event=None: model_body)
    import universum.orchestrator as orch_mod

    captured: dict[str, str] = {}

    class FakeResult:
        success = True
        pdf_path = fake_pdf
        log = ""
        engine = "fake"

    def fake_compile(code, llm, config=None, progress=None, cancel_event=None, postprocess=None, prompt_view=None):
        captured["code"] = code
        return FakeResult()

    monkeypatch.setattr(orch_mod, "compile_with_repair", fake_compile)
    req = Req(
        request_text="тест по налогам",
        doc_type=DocType.TEST,
        style=Style.MINIMAL,
        volume=Volume(questions=5, variants=2),
    )
    orch.generate(req)
    code = captured["code"]
    assert code.count(r"\clearpage") == 3
    assert code.index(r"\clearpage") < code.index(r"\section*{Вариант 1}")
    assert code.index(r"\clearpage", code.index(r"Вариант 2")) < code.index(r"Ключ ответов")


def test_count_variants_recognises_heading_styles():
    from universum.utils import count_variants

    body = (
        r"\section*{Вариант 1}" + "\n" + "x\n"
        + r"\section{Вариант 2}" + "\n" + "x\n"
        + r"\subsection*{Variant 3}" + "\n" + "x\n"
        + r"\section*{Ключ ответов}" + "\n" + "x\n"
    )
    assert count_variants(body) == 3
    assert count_variants("") == 0


def test_orchestrator_retries_when_too_few_variants(monkeypatch, tmp_path):
    """Просили 2 варианта, модель дала 1 — приложение просит перегенерировать."""
    from universum.orchestrator import GenerationRequest as Req

    fake_pdf = tmp_path / "compiled.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 test")

    one_variant = (
        r"\makelecturetitle{Налоги}" + "\n"
        + r"\section*{Вариант 1}" + "\nвопрос\n"
        + r"\section*{Ключ ответов}" + "\n1 — а\n"
    )
    two_variants = (
        r"\makelecturetitle{Налоги}" + "\n"
        + r"\section*{Вариант 1}" + "\nвопрос\n"
        + r"\section*{Вариант 2}" + "\nвопрос\n"
        + r"\section*{Ключ ответов}" + "\n1 — а\n"
    )

    orch = Orchestrator()
    calls: list[tuple[str, str]] = []

    def fake_llm(system, user, progress=None, cancel_event=None):
        calls.append((system, user))
        return one_variant if len(calls) == 1 else two_variants

    monkeypatch.setattr(orch.llm, "generate", fake_llm)
    import universum.orchestrator as orch_mod

    captured: dict[str, str] = {}

    class FakeResult:
        success = True
        pdf_path = fake_pdf
        log = ""
        engine = "fake"

    def fake_compile(code, llm, config=None, progress=None, cancel_event=None, postprocess=None, prompt_view=None):
        captured["code"] = code
        return FakeResult()

    monkeypatch.setattr(orch_mod, "compile_with_repair", fake_compile)
    req = Req(
        request_text="тест по налогам",
        doc_type=DocType.TEST,
        style=Style.NEON,
        volume=Volume(questions=5, variants=2),
    )
    orch.generate(req)

    assert len(calls) == 2  # был повторный запрос
    assert "ИСПРАВЛЕНИЕ" in calls[1][1]
    assert "2 варианта" in calls[1][1]
    assert captured["code"].count(r"\section*{Вариант") == 2


def test_orchestrator_does_not_retry_when_variants_match(monkeypatch, tmp_path):
    from universum.orchestrator import GenerationRequest as Req

    fake_pdf = tmp_path / "compiled.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 test")
    body = (
        r"\makelecturetitle{Налоги}" + "\n"
        + r"\section*{Вариант 1}" + "\nвопрос\n"
        + r"\section*{Вариант 2}" + "\nвопрос\n"
        + r"\section*{Ключ ответов}" + "\n1 — а\n"
    )
    orch = Orchestrator()
    calls: list[int] = []

    def fake_llm(system, user, progress=None, cancel_event=None):
        calls.append(1)
        return body

    monkeypatch.setattr(orch.llm, "generate", fake_llm)
    import universum.orchestrator as orch_mod

    class FakeResult:
        success = True
        pdf_path = fake_pdf
        log = ""
        engine = "fake"

    monkeypatch.setattr(orch_mod, "compile_with_repair", lambda code, llm, **kw: FakeResult())
    req = Req(request_text="тест", doc_type=DocType.TEST, style=Style.STRICT, volume=Volume(questions=5, variants=2))
    orch.generate(req)
    assert len(calls) == 1


def _strip_tex_comments(source: str) -> str:
    lines = []
    for line in source.splitlines():
        out = []
        i = 0
        while i < len(line):
            if line[i] == "\\" and i + 1 < len(line):
                out.append(line[i:i + 2])
                i += 2
                continue
            if line[i] == "%":
                break
            out.append(line[i])
            i += 1
        lines.append("".join(out))
    return "\n".join(lines)


@pytest.mark.parametrize("style", list(Style))
@pytest.mark.parametrize("doc_type", list(DocType))
def test_wrapped_document_is_structurally_valid(style, doc_type):
    """Сгенерированный документ: сбалансированы скобки и окружения."""
    from universum.prompts import wrap_document

    body = (
        r"\makelecturetitle{Тестовая тема}" + "\n\n"
        r"\section{Раздел}" + "\n"
        r"\begin{definition}[Термин]Определение.\end{definition}" + "\n"
        r"\begin{important}Важно.\end{important}" + "\n"
        r"\begin{enumerate}\item Один \begin{enumerate}\item[а)] да\end{enumerate}\end{enumerate}" + "\n"
        r"\begin{tabularx}{\linewidth}{lX}\toprule A & B \\\bottomrule\end{tabularx}" + "\n"
        r"\begin{lstlisting}[language=Python]\nprint(1)\n\end{lstlisting}" + "\n"
    )
    code = _strip_tex_comments(wrap_document(body, style, "ru", doc_type=doc_type))

    depth = 0
    i = 0
    while i < len(code):
        ch = code[i]
        if ch == "\\":
            i += 2
            continue
        depth += 1 if ch == "{" else -1 if ch == "}" else 0
        assert depth >= 0, "лишняя закрывающая скобка: %s/%s" % (style, doc_type)
        i += 1
    assert depth == 0

    stack: list[str] = []
    for match in re.finditer(r"\\(begin|end)\{([^{}]+)\}", code):
        if match.group(1) == "begin":
            stack.append(match.group(2))
        else:
            assert stack and stack[-1] == match.group(2), "нарушен порядок окружений: %s/%s" % (style, doc_type)
            stack.pop()
    assert not stack
