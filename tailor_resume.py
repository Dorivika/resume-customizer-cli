#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from enum import Enum
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import click
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import SpinnerColumn, TextColumn, Progress
from rich.prompt import Prompt
from rich.table import Table

APP_NAME = "resume-tailor"
DEFAULT_ROUTE = "openrouter-free,gemini,openai"
DEFAULT_OPENAI_MODEL = "gpt-5.3-codex"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENROUTER_MODEL = "auto-free"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPT = PROJECT_DIR / "prompt.md"
DEFAULT_TEMPLATE_DIR = PROJECT_DIR / "templates"
VALID_PROVIDERS = {"openrouter-free", "gemini", "openai"}
PAID_PROVIDERS = {"gemini", "openai"}
PROVIDER_KEY_NAMES = ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY")
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
CONSOLE = Console()
ERROR_CONSOLE = Console(stderr=True)
RENDER_MACROS = r"""
\newcommand{\resumeEntry}[4]{
  \textbf{#1} \hfill #4\\
  #2 \hfill #3
}
""".strip()

app = typer.Typer(
    name=APP_NAME,
    help="AI-tailor a resume, render LaTeX, and compile PDF.",
    add_completion=False,
    rich_markup_mode="rich",
)


class PaidFallback(str, Enum):
    ask = "ask"
    auto = "auto"
    never = "never"


class OutputFormat(str, Enum):
    tex = "tex"
    pdf = "pdf"
    both = "both"


class ProviderError(RuntimeError):
    pass


class RouteBlocked(RuntimeError):
    pass


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated_keys = set(updates)
    result = []
    seen = set()
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            result.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            result.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            result.append(line)
    additions = [key for key in updates if key not in seen]
    if additions and result and result[-1].strip():
        result.append("")
    for key in additions:
        result.append(f"{key}={updates[key]}")
    path.write_text("\n".join(result).rstrip() + "\n", encoding="utf-8")
    for key in updated_keys:
        os.environ[key] = updates[key]


def provider_keys_from_env(env_path: Path = Path(".env")) -> dict[str, str]:
    values = read_env_file(env_path)
    found = {}
    for key in PROVIDER_KEY_NAMES:
        value = os.environ.get(key) or values.get(key)
        if value:
            found[key] = value
    return found


def has_any_provider_key(env_path: Path = Path(".env")) -> bool:
    return bool(provider_keys_from_env(env_path))


def cache_dir() -> Path:
    base = (
        os.environ.get("XDG_CACHE_HOME")
        or os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or str(Path.home() / ".cache")
    )
    path = Path(base) / "resume-tailor"
    path.mkdir(parents=True, exist_ok=True)
    return path


def template_path(value: str | Path, template_dir: Path = DEFAULT_TEMPLATE_DIR) -> Path:
    path = Path(value)
    if path.exists():
        return path
    named = template_dir / f"{path.stem}.tex"
    if named.exists():
        return named
    raise FileNotFoundError(f"Template not found: {value}")


def read_job_from_cli() -> str:
    CONSOLE.print("[bold cyan]Paste the full job description below.[/bold cyan]")
    CONSOLE.print("Finish with a line containing only: [bold].done[/bold]")
    CONSOLE.print("Or type [bold]/file path/to/job.txt[/bold] on the first line to load a file.")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not lines and line.strip().lower().startswith("/file "):
            return read_document_text(Path(line.strip()[6:].strip()))
        if line.strip() == ".done":
            break
        lines.append(line)
    job = "\n".join(lines).strip()
    if not job:
        raise ValueError("No job description was provided.")
    return job


def build_prompt(prompt_template: Path, resume: str, job: str) -> str:
    template = read_text(prompt_template)
    return template.replace("{{RESUME}}", resume).replace("{{JOB_DESCRIPTION}}", job)


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Request failed: {exc.reason}") from exc


def get_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Request failed: {exc.reason}") from exc


def call_openai(prompt: str, model: str, timeout: int) -> tuple[str, str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ProviderError("OPENAI_API_KEY is not set.")
    payload = {
        "model": model,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "text": {"format": {"type": "json_object"}},
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    if os.environ.get("OPENAI_ORG_ID"):
        headers["OpenAI-Organization"] = os.environ["OPENAI_ORG_ID"]
    if os.environ.get("OPENAI_PROJECT_ID"):
        headers["OpenAI-Project"] = os.environ["OPENAI_PROJECT_ID"]
    data = post_json("https://api.openai.com/v1/responses", headers, payload, timeout)
    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                parts.append(content.get("text", ""))
    if not parts and "output_text" in data:
        parts.append(data["output_text"])
    if not parts:
        raise ProviderError("OpenAI response did not include text output.")
    return "\n".join(parts), model


def call_gemini(prompt: str, model: str, timeout: int) -> tuple[str, str]:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ProviderError("GEMINI_API_KEY or GOOGLE_API_KEY is not set.")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    data = post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": api_key},
        payload,
        timeout,
    )
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"], model
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"Gemini response did not include text output: {data}") from exc


def openrouter_headers(api_key_env: str) -> dict[str, str]:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ProviderError(f"{api_key_env} is not set.")
    headers = {"Authorization": f"Bearer {api_key}"}
    if os.environ.get("OPENROUTER_REFERER"):
        headers["HTTP-Referer"] = os.environ["OPENROUTER_REFERER"]
    if os.environ.get("OPENROUTER_APP_TITLE"):
        headers["X-Title"] = os.environ["OPENROUTER_APP_TITLE"]
    return headers


def cached_openrouter_models(headers: dict[str, str], refresh: bool, timeout: int) -> list[dict[str, Any]]:
    cache_file = cache_dir() / "openrouter_models.json"
    if not refresh and cache_file.exists():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - payload.get("fetched_at", 0) < 86400:
                return payload.get("data", [])
        except (json.JSONDecodeError, OSError):
            pass
    data = get_json(OPENROUTER_MODELS_URL, headers, timeout)
    models = data.get("data", [])
    cache_file.write_text(json.dumps({"fetched_at": time.time(), "data": models}, indent=2), encoding="utf-8")
    return models


def price_is_free(pricing: dict[str, Any]) -> bool:
    fields = ["prompt", "completion", "request", "image", "web_search", "internal_reasoning"]
    values = [pricing.get(field) for field in fields if field in pricing and pricing.get(field) is not None]
    return bool(values) and all(str(value) == "0" for value in values)


def free_model_score(model: dict[str, Any]) -> tuple[int, int, int, int, int, str]:
    supported = set(model.get("supported_parameters") or [])
    model_id = str(model.get("id", ""))
    name = str(model.get("name", ""))
    text = f"{model_id} {name}".lower()
    context = int(model.get("context_length") or 0)
    top_provider = model.get("top_provider") or {}
    max_completion = int(top_provider.get("max_completion_tokens") or 0)
    structured = 1 if {"structured_outputs", "response_format"} & supported else 0
    reasoning = 1 if {"reasoning", "include_reasoning"} & supported else 0
    family_boost = 0
    for marker, boost in [
        ("qwen", 9),
        ("deepseek", 8),
        ("kimi", 8),
        ("llama", 7),
        ("gemini", 7),
        ("mistral", 6),
        ("nemotron", 6),
        ("glm", 6),
    ]:
        if marker in text:
            family_boost = max(family_boost, boost)
    created = int(model.get("created") or 0)
    return structured, reasoning, family_boost, context, max_completion, f"{created:020d}:{model_id}"


def ranked_free_openrouter_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for model in models:
        model_id = str(model.get("id", ""))
        architecture = model.get("architecture") or {}
        output_modalities = architecture.get("output_modalities") or []
        if "text" not in output_modalities:
            continue
        if not (model_id.endswith(":free") or price_is_free(model.get("pricing") or {})):
            continue
        if not price_is_free(model.get("pricing") or {}):
            continue
        supported = set(model.get("supported_parameters") or [])
        if not ({"structured_outputs", "response_format"} & supported):
            continue
        candidates.append(model)
    return sorted(candidates, key=free_model_score, reverse=True)


def select_openrouter_model(
    requested_model: str,
    api_key_env: str,
    refresh_models: bool,
    timeout: int,
) -> tuple[str, list[dict[str, Any]]]:
    if requested_model != "auto-free":
        return requested_model, []
    headers = openrouter_headers(api_key_env)
    ranked = ranked_free_openrouter_models(cached_openrouter_models(headers, refresh_models, timeout))
    if not ranked:
        raise ProviderError("No free OpenRouter text models were found in the Models API response.")
    return str(ranked[0]["id"]), ranked[:5]


def call_openrouter(
    prompt: str,
    model: str,
    api_key_env: str,
    refresh_models: bool,
    timeout: int,
) -> tuple[str, str, list[dict[str, Any]]]:
    selected_model, ranked = select_openrouter_model(model, api_key_env, refresh_models, timeout)
    payload = {
        "model": selected_model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    data = post_json(OPENROUTER_CHAT_URL, openrouter_headers(api_key_env), payload, timeout)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"OpenRouter response did not include text output: {data}") from exc
    return content, str(data.get("model") or selected_model), ranked


def parse_route(route: str) -> list[str]:
    providers = [item.strip() for item in route.split(",") if item.strip()]
    if not providers:
        raise ValueError("Route must contain at least one provider.")
    unknown = [provider for provider in providers if provider not in VALID_PROVIDERS]
    if unknown:
        raise ValueError(f"Unknown route provider(s): {', '.join(unknown)}")
    return providers


def route_is_paid(provider: str) -> bool:
    return provider in PAID_PROVIDERS


def provider_has_key(provider: str, openrouter_api_key_env: str) -> bool:
    if provider == "openrouter-free":
        return bool(os.environ.get(openrouter_api_key_env))
    if provider == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    return False


def confirm_paid_fallback(provider: str, mode: PaidFallback) -> None:
    if not route_is_paid(provider):
        return
    if mode == PaidFallback.auto:
        return
    if mode == PaidFallback.never:
        raise RouteBlocked(f"Paid fallback blocked before provider: {provider}")
    if not typer.confirm(f"Fallback provider '{provider}' may incur API costs. Continue?", default=False):
        raise RouteBlocked(f"User declined paid fallback to provider: {provider}")


def call_route(
    route: list[str],
    prompt: str,
    paid_fallback: PaidFallback,
    openrouter_model: str,
    openrouter_api_key_env: str,
    refresh_openrouter_models: bool,
    openai_model: str,
    gemini_model: str,
    timeout: int,
) -> tuple[str, str, str, list[dict[str, str]], list[dict[str, Any]]]:
    trace: list[dict[str, str]] = []
    ranked: list[dict[str, Any]] = []
    for index, provider in enumerate(route):
        if not provider_has_key(provider, openrouter_api_key_env):
            trace.append({"provider": provider, "model": "", "status": "skipped", "detail": "provider key is not configured"})
            continue
        if index > 0:
            confirm_paid_fallback(provider, paid_fallback)
        try:
            if provider == "openrouter-free":
                raw, model_used, ranked = call_openrouter(
                    prompt,
                    openrouter_model,
                    openrouter_api_key_env,
                    refresh_openrouter_models,
                    timeout,
                )
            elif provider == "gemini":
                raw, model_used = call_gemini(prompt, gemini_model, timeout)
            elif provider == "openai":
                raw, model_used = call_openai(prompt, openai_model, timeout)
            else:
                raise ProviderError(f"Unsupported provider: {provider}")
            trace.append({"provider": provider, "model": model_used, "status": "success", "detail": ""})
            return raw, provider, model_used, trace, ranked
        except ProviderError as exc:
            trace.append({"provider": provider, "model": "", "status": "failed", "detail": str(exc)})
    failures = "\n".join(f"{item['provider']}: {item['detail']}" for item in trace)
    raise ProviderError("No provider succeeded:\n" + failures)


def extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Provider returned JSON, but the root value was not an object.")
    return value


def require_fields(data: dict[str, Any]) -> None:
    required = ["name", "headline", "contact", "summary", "skills", "experience", "education", "keyword_report", "decisions"]
    missing = [field for field in required if field not in data]
    if missing:
        raise ValueError(f"Tailored resume JSON is missing required fields: {', '.join(missing)}")


def latex_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def section(title: str, body: list[str]) -> str:
    if not body:
        return ""
    return "\\section*{" + latex_escape(title) + "}\n" + "\n".join(body)


def render_body(data: dict[str, Any]) -> str:
    blocks = []
    if data.get("summary"):
        blocks.append(section("Summary", [latex_escape(data["summary"])]))

    skill_lines = []
    for group in data.get("skills", []):
        items = ", ".join(latex_escape(item) for item in group.get("items", []))
        if items:
            skill_lines.append(r"\textbf{" + latex_escape(group.get("group", "Skills")) + r":} " + items + r"\\")
    blocks.append(section("Skills", skill_lines))

    experience_lines = []
    for job in data.get("experience", []):
        header = (
            r"\resumeEntry{"
            + latex_escape(job.get("title", ""))
            + "}{"
            + latex_escape(job.get("company", ""))
            + "}{"
            + latex_escape(job.get("location", ""))
            + "}{"
            + latex_escape(job.get("dates", ""))
            + "}"
        )
        bullets = "\n".join(r"\item " + latex_escape(bullet) for bullet in job.get("bullets", []))
        experience_lines.append(header + "\n\\begin{itemize}\n" + bullets + "\n\\end{itemize}")
    blocks.append(section("Experience", experience_lines))

    project_lines = []
    for project in data.get("projects", []):
        title = r"\textbf{" + latex_escape(project.get("name", "")) + r"}"
        if project.get("description"):
            title += " -- " + latex_escape(project["description"])
        bullets = "\n".join(r"\item " + latex_escape(bullet) for bullet in project.get("bullets", []))
        project_lines.append(title + "\n\\begin{itemize}\n" + bullets + "\n\\end{itemize}")
    blocks.append(section("Projects", project_lines))

    education_lines = []
    for item in data.get("education", []):
        education_lines.append(
            r"\resumeEntry{"
            + latex_escape(item.get("credential", ""))
            + "}{"
            + latex_escape(item.get("school", ""))
            + "}{"
            + latex_escape(item.get("location", ""))
            + "}{"
            + latex_escape(item.get("dates", ""))
            + "}"
        )
    blocks.append(section("Education", education_lines))

    return "\n\n".join(block for block in blocks if block)


def keyword_comment(data: dict[str, Any]) -> str:
    report = data.get("keyword_report", {})
    lines = ["Keyword report generated by the model:"]
    for name in ["matched_keywords", "missing_keywords", "tailoring_notes"]:
        values = report.get(name, [])
        lines.append(f"{name}: {', '.join(str(item) for item in values)}")
    return "\n".join("% " + line for line in lines)


def render_latex(data: dict[str, Any], template_file: Path) -> str:
    template = template_file.read_text(encoding="utf-8")
    replacements = {
        "{{NAME}}": latex_escape(data.get("name", "")),
        "{{HEADLINE}}": latex_escape(data.get("headline", "")),
        "{{CONTACT}}": latex_escape(" | ".join(data.get("contact", []))),
        "{{RESUME_BODY}}": render_body(data),
        "{{KEYWORD_REPORT_COMMENT}}": keyword_comment(data),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def show_route(route: list[str], paid_fallback: PaidFallback) -> None:
    table = Table(title="Request Route")
    table.add_column("#", style="dim")
    table.add_column("Provider", style="cyan")
    table.add_column("Cost class")
    for index, provider in enumerate(route, start=1):
        table.add_row(str(index), provider, "paid fallback" if route_is_paid(provider) else "free route")
    CONSOLE.print(table)
    CONSOLE.print(f"[dim]Paid fallback mode:[/dim] [bold]{paid_fallback.value}[/bold]")


def show_ranked_models(ranked: list[dict[str, Any]]) -> None:
    if not ranked:
        return
    table = Table(title="Top OpenRouter Free Candidates")
    table.add_column("Rank", style="dim")
    table.add_column("Model")
    table.add_column("Context", justify="right")
    table.add_column("JSON")
    for index, model in enumerate(ranked, start=1):
        supported = set(model.get("supported_parameters") or [])
        table.add_row(
            str(index),
            str(model.get("id", "")),
            str(model.get("context_length") or ""),
            "yes" if {"structured_outputs", "response_format"} & supported else "no",
        )
    CONSOLE.print(table)


def show_trace(trace: list[dict[str, str]]) -> None:
    table = Table(title="Provider Trace")
    table.add_column("Provider", style="cyan")
    table.add_column("Model")
    table.add_column("Status")
    table.add_column("Detail")
    for item in trace:
        if item["status"] == "success":
            status = "[green]success[/green]"
        elif item["status"] == "skipped":
            status = "[yellow]skipped[/yellow]"
        else:
            status = "[red]failed[/red]"
        table.add_row(item["provider"], item["model"], status, item["detail"])
    CONSOLE.print(table)


def show_decisions(data: dict[str, Any]) -> None:
    decisions = data.get("decisions", [])
    if not decisions:
        CONSOLE.print("[yellow]No tailoring decisions were returned.[/yellow]")
        return
    table = Table(title="Tailoring Decisions", show_lines=True)
    table.add_column("Decision", style="bold")
    table.add_column("Source", overflow="fold")
    table.add_column("Reason", overflow="fold")
    table.add_column("Final", overflow="fold")
    styles = {"KEEP": "green", "DROP": "red", "REWRITE": "yellow", "COMBINE": "magenta"}
    for item in decisions:
        action = str(item.get("decision", "NOTE")).upper()
        style = styles.get(action, "cyan")
        table.add_row(
            f"[{style}]{action}[/{style}]",
            str(item.get("source", "")),
            str(item.get("reason", "")),
            str(item.get("after", "")),
        )
    CONSOLE.print(table)


def show_outputs(files: list[Path]) -> None:
    table = Table(title="Output")
    table.add_column("File")
    for file in files:
        table.add_row(str(file))
    CONSOLE.print(table)


def key_status_table(env_path: Path) -> None:
    values = provider_keys_from_env(env_path)
    table = Table(title="Provider Key Status")
    table.add_column("Provider", style="cyan")
    table.add_column("Key")
    table.add_row("OpenRouter", "configured" if values.get("OPENROUTER_API_KEY") else "missing")
    table.add_row("Gemini", "configured" if values.get("GEMINI_API_KEY") or values.get("GOOGLE_API_KEY") else "missing")
    table.add_row("OpenAI", "configured" if values.get("OPENAI_API_KEY") else "missing")
    CONSOLE.print(table)


def find_latex_engine(engine: str | None) -> str | None:
    candidates = [engine] if engine else ["tectonic", "latexmk", "pdflatex", "xelatex", "lualatex"]
    return next((candidate for candidate in candidates if candidate and shutil.which(candidate)), None)


def diagnostic_status_table(title: str, rows: list[tuple[str, bool, str]]) -> bool:
    table = Table(title=title)
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    passed = True
    for name, ok, detail in rows:
        passed = passed and ok
        table.add_row(name, "[green]ok[/green]" if ok else "[red]fail[/red]", detail)
    CONSOLE.print(table)
    return passed


def diagnose_document(label: str, path: Path | None, required: bool) -> tuple[str, bool, str]:
    if path is None:
        return label, not required, "not provided" if not required else "required path was not provided"
    try:
        text = read_document_text(path)
    except Exception as exc:
        return label, False, str(exc)
    word_count = len(re.findall(r"\S+", text))
    return label, True, f"{path} ({word_count} words)"


def diagnose_route_config(route: str, openrouter_api_key_env: str) -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    try:
        providers = parse_route(route)
        rows.append(("route syntax", True, " -> ".join(providers)))
    except ValueError as exc:
        return [("route syntax", False, str(exc))]

    for provider in providers:
        if provider == "openrouter-free":
            key_name = openrouter_api_key_env
        elif provider == "gemini":
            key_name = "GEMINI_API_KEY or GOOGLE_API_KEY"
        else:
            key_name = "OPENAI_API_KEY"
        configured = provider_has_key(provider, openrouter_api_key_env)
        rows.append((f"{provider} key", configured, "configured" if configured else f"missing {key_name}"))
    return rows


@app.command("diagnose")
def diagnose(
    resume: Path | None = typer.Option(None, "--resume", help="Path to a resume to verify."),
    job: Path | None = typer.Option(None, "--job", help="Path to a job description to verify."),
    env: Path = typer.Option(Path(".env"), "--env", help="Optional .env path."),
    route: str = typer.Option(DEFAULT_ROUTE, "--route", help="Comma-separated provider route."),
    openrouter_api_key_env: str = typer.Option("OPENROUTER_API_KEY", "--openrouter-api-key-env", help="Environment variable containing the OpenRouter key."),
    template: str = typer.Option("classic", "--template", help="Template name from templates/ or a path to a .tex file."),
    template_dir: Path = typer.Option(DEFAULT_TEMPLATE_DIR, "--template-dir", help="Template directory."),
    output_dir: Path = typer.Option(Path("output"), "--output-dir", help="Output directory to check."),
    latex_engine: str | None = typer.Option(None, "--latex-engine", help="Preferred LaTeX engine to check."),
    require_pdf: bool = typer.Option(False, "--require-pdf", help="Fail diagnostics when no PDF engine is available."),
    strict: bool = typer.Option(False, "--strict/--no-strict", help="Exit non-zero when any check fails."),
) -> None:
    load_dotenv(env)
    if route == DEFAULT_ROUTE:
        route = os.environ.get("RESUME_TAILOR_ROUTE", route)

    rows = [
        diagnose_document("resume input", resume, required=resume is not None),
        diagnose_document("job description", job, required=job is not None),
    ]
    try:
        selected_template = template_path(template, template_dir)
        rows.append(("template", True, str(selected_template)))
    except FileNotFoundError as exc:
        rows.append(("template", False, str(exc)))

    output_parent = output_dir.resolve().parent
    rows.append(("output directory", output_parent.exists(), f"parent exists: {output_parent}"))

    selected_engine = find_latex_engine(latex_engine)
    rows.append(
        (
            "PDF engine",
            selected_engine is not None or not require_pdf,
            selected_engine or "not found; LaTeX output can still be generated",
        )
    )

    ok = diagnostic_status_table("Local Inputs", rows)
    route_ok = diagnostic_status_table("Provider Route", diagnose_route_config(route, openrouter_api_key_env))
    if strict and not (ok and route_ok):
        raise RuntimeError("Diagnostics failed.")


def setup_wizard(env_path: Path = Path(".env"), force: bool = False) -> bool:
    load_dotenv(env_path)
    if has_any_provider_key(env_path) and not force:
        CONSOLE.print("[green]Provider key already configured.[/green]")
        key_status_table(env_path)
        return True

    CONSOLE.print(Panel.fit("[bold cyan]First-run setup[/bold cyan]\nAdd provider keys. Each key is optional, but at least one is required to tailor a resume."))
    existing = read_env_file(env_path)
    updates: dict[str, str] = {
        "RESUME_TAILOR_ROUTE": existing.get("RESUME_TAILOR_ROUTE", DEFAULT_ROUTE),
        "RESUME_TAILOR_PAID_FALLBACK": existing.get("RESUME_TAILOR_PAID_FALLBACK", PaidFallback.ask.value),
        "OPENROUTER_MODEL": existing.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        "OPENROUTER_APP_TITLE": existing.get("OPENROUTER_APP_TITLE", "Resume Tailor"),
    }
    prompts = [
        ("OPENROUTER_API_KEY", "OpenRouter API key"),
        ("GEMINI_API_KEY", "Gemini API key"),
        ("OPENAI_API_KEY", "OpenAI API key"),
    ]
    for key, label in prompts:
        current = os.environ.get(key) or existing.get(key)
        suffix = " [already configured, press Enter to keep]" if current else " [optional, press Enter to skip]"
        value = Prompt.ask(label + suffix, password=sys.stdin.isatty(), default="")
        if value.strip():
            updates[key] = value.strip()
        elif current:
            updates[key] = current

    if not any(updates.get(key) for key in PROVIDER_KEY_NAMES):
        CONSOLE.print("[yellow]No provider keys were saved. Add at least one key before running tailoring.[/yellow]")
        return False
    write_env_values(env_path, updates)
    CONSOLE.print(f"[green]Saved setup to[/green] {env_path.resolve()}")
    key_status_table(env_path)
    return True


def compile_pdf(tex_file: Path, output_dir: Path, engine: str | None, require_pdf: bool) -> Path | None:
    selected = find_latex_engine(engine)
    if not selected:
        message = "No LaTeX engine found. Install Tectonic, TeX Live, or MiKTeX to compile PDF."
        if require_pdf:
            raise RuntimeError(message)
        CONSOLE.print(f"[yellow]{message}[/yellow]")
        return None

    if selected == "tectonic":
        command = [selected, str(tex_file), "--outdir", str(output_dir)]
    elif selected == "latexmk":
        command = [selected, "-pdf", "-interaction=nonstopmode", f"-outdir={output_dir}", str(tex_file)]
    else:
        command = [selected, "-interaction=nonstopmode", "-output-directory", str(output_dir), str(tex_file)]

    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        if require_pdf:
            raise RuntimeError(f"PDF compilation failed:\n{message}")
        CONSOLE.print("[yellow]PDF compilation failed. LaTeX source was still written.[/yellow]")
        return None

    pdf = output_dir / f"{tex_file.stem}.pdf"
    if pdf.exists():
        return pdf
    CONSOLE.print("[yellow]LaTeX engine completed but the expected PDF was not found.[/yellow]")
    return None


def extract_tex_template(source: Path, destination: Path) -> None:
    text = source.read_text(encoding="utf-8")
    if all(token in text for token in ["{{NAME}}", "{{RESUME_BODY}}"]):
        if r"\resumeEntry" not in text:
            text = text.replace(r"\begin{document}", RENDER_MACROS + "\n\n" + r"\begin{document}", 1)
        destination.write_text(text, encoding="utf-8")
        return

    begin = text.find(r"\begin{document}")
    end = text.rfind(r"\end{document}")
    if begin == -1 or end == -1:
        raise ValueError("LaTeX source must contain \\begin{document} and \\end{document}.")
    preamble = text[: begin + len(r"\begin{document}")]
    if r"\resumeEntry" not in preamble:
        preamble = preamble.replace(r"\begin{document}", RENDER_MACROS + "\n\n" + r"\begin{document}", 1)
    template = f"""{preamble}

\\begin{{center}}
{{\\LARGE \\textbf{{ {{{{NAME}}}} }}}}\\\\
{{{{HEADLINE}}}}\\\\
{{{{CONTACT}}}}
\\end{{center}}

{{{{RESUME_BODY}}}}

{{{{KEYWORD_REPORT_COMMENT}}}}

\\end{{document}}
"""
    destination.write_text(template, encoding="utf-8")


def derive_tex_template(source: Path, destination: Path) -> Path:
    extract_tex_template(source, destination)
    return destination


def extract_pdf_text(source: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        completed = subprocess.run([pdftotext, "-layout", str(source), "-"], text=True, capture_output=True, check=False)
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()

    if importlib.util.find_spec("pypdf"):
        from pypdf import PdfReader

        reader = PdfReader(str(source))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()

    return ""


def extract_docx_text(source: Path) -> str:
    with zipfile.ZipFile(source) as archive:
        try:
            xml = archive.read("word/document.xml")
        except KeyError as exc:
            raise ValueError(f"{source} is not a readable .docx file.") from exc
    root = ElementTree.fromstring(xml)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", namespace):
        parts = [node.text for node in paragraph.findall(".//w:t", namespace) if node.text]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs).strip()


def extract_doc_text(source: Path) -> str:
    commands = [
        ["antiword", str(source)],
        ["catdoc", str(source)],
        ["textutil", "-convert", "txt", "-stdout", str(source)],
    ]
    for command in commands:
        if not shutil.which(command[0]):
            continue
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    raise ValueError(
        "Legacy .doc extraction requires antiword, catdoc, or macOS textutil. "
        "Convert the file to .docx or PDF if none of those tools are installed."
    )


def read_document_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt", ".tex", ".text", ".rst"}:
        return read_text(path)
    if suffix == ".pdf":
        text = extract_pdf_text(path)
        if not text:
            raise ValueError("Could not extract text from PDF. Install pdftotext or `python -m pip install -e .[pdf]`.")
        return text
    if suffix == ".docx":
        text = extract_docx_text(path)
        if not text:
            raise ValueError(f"No text was extracted from {path}.")
        return text
    if suffix == ".doc":
        return extract_doc_text(path)
    raise ValueError(f"Unsupported document format: {suffix}. Use .md, .txt, .tex, .pdf, .docx, or .doc.")


def extract_pdf_template(source: Path, destination: Path) -> str:
    text = extract_pdf_text(source)
    note = (
        "PDF files do not contain recoverable LaTeX source. "
        "This template preserves extracted text as comments and uses the default render layout."
    )
    escaped_comment = "\n".join("% " + line for line in ([note, "", "Extracted PDF text:"] + text.splitlines()))
    base = (DEFAULT_TEMPLATE_DIR / "classic.tex").read_text(encoding="utf-8")
    destination.write_text(escaped_comment + "\n\n" + base, encoding="utf-8")
    return note


@app.command()
def tailor(
    resume: Path | None = typer.Option(None, "--resume", help="Path to your source resume as .md, .txt, .tex, .pdf, .docx, or .doc."),
    job: Path | None = typer.Option(None, "--job", help="Path to a job description. Omit this to paste interactively."),
    prompt: Path = typer.Option(DEFAULT_PROMPT, "--prompt", help="Prompt template path."),
    template: str | None = typer.Option(None, "--template", help="Template name from templates/ or a path to a .tex file."),
    template_dir: Path = typer.Option(DEFAULT_TEMPLATE_DIR, "--template-dir", help="Template directory."),
    output_dir: Path = typer.Option(Path("output"), "--output-dir", help="Output directory."),
    env: Path = typer.Option(Path(".env"), "--env", help="Optional .env path."),
    route: str = typer.Option(DEFAULT_ROUTE, "--route", help="Comma-separated provider route."),
    paid_fallback: PaidFallback = typer.Option(PaidFallback.ask, "--paid-fallback", help="How to handle fallback into paid providers."),
    openrouter_api_key_env: str = typer.Option("OPENROUTER_API_KEY", "--openrouter-api-key-env", help="Environment variable containing the OpenRouter key."),
    openrouter_model: str = typer.Option(DEFAULT_OPENROUTER_MODEL, "--openrouter-model", help="OpenRouter model ID or auto-free."),
    refresh_openrouter_models: bool = typer.Option(False, "--refresh-openrouter-models/--no-refresh-openrouter-models", help="Refresh OpenRouter model metadata."),
    show_route_flag: bool = typer.Option(False, "--show-route/--no-show-route", help="Print route details before running."),
    openai_model: str = typer.Option(DEFAULT_OPENAI_MODEL, "--openai-model", help="OpenAI model."),
    gemini_model: str = typer.Option(DEFAULT_GEMINI_MODEL, "--gemini-model", help="Gemini model."),
    timeout: int = typer.Option(int(os.environ.get("RESUME_TAILOR_TIMEOUT", "240")), "--timeout", help="Request timeout in seconds."),
    latex_engine: str | None = typer.Option(None, "--latex-engine", help="LaTeX engine: tectonic, latexmk, pdflatex, xelatex, or lualatex."),
    output_format: OutputFormat = typer.Option(OutputFormat.both, "--output-format", help="Output format to produce."),
    preserve_tex_format: bool = typer.Option(True, "--preserve-tex-format/--no-preserve-tex-format", help="Use a .tex resume as the output template when no explicit template is provided."),
    no_pdf: bool = typer.Option(False, "--no-pdf", help="Write LaTeX but skip PDF compilation."),
    require_pdf: bool = typer.Option(False, "--require-pdf", help="Fail if PDF compilation is unavailable or fails."),
) -> None:
    load_dotenv(env)
    if not has_any_provider_key(env):
        raise ProviderError("No provider key is configured. Run `resume-tailor setup` or set OPENROUTER_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY, or OPENAI_API_KEY.")
    if no_pdf:
        output_format = OutputFormat.tex
    if route == DEFAULT_ROUTE:
        route = os.environ.get("RESUME_TAILOR_ROUTE", route)
    if paid_fallback == PaidFallback.ask:
        paid_fallback = PaidFallback(os.environ.get("RESUME_TAILOR_PAID_FALLBACK", paid_fallback.value))
    if openrouter_model == DEFAULT_OPENROUTER_MODEL:
        openrouter_model = os.environ.get("OPENROUTER_MODEL", openrouter_model)
    if openai_model == DEFAULT_OPENAI_MODEL:
        openai_model = os.environ.get("OPENAI_MODEL", openai_model)
    if gemini_model == DEFAULT_GEMINI_MODEL:
        gemini_model = os.environ.get("GEMINI_MODEL", gemini_model)
    route_items = parse_route(route)
    resume_path = resume or (Path(os.environ["RESUME_TAILOR_RESUME"]) if os.environ.get("RESUME_TAILOR_RESUME") else None)
    if not resume_path:
        raise typer.BadParameter("Provide --resume or set RESUME_TAILOR_RESUME.")

    CONSOLE.print(Panel.fit("[bold cyan]resume-tailor[/bold cyan]\nAI resume tailoring with routed model fallback"))
    if show_route_flag:
        show_route(route_items, paid_fallback)

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=CONSOLE, transient=True) as progress:
        task = progress.add_task("Loading resume and job description", total=None)
        resume_text = read_document_text(resume_path)
        job_text = read_document_text(job) if job else read_job_from_cli()
        rendered_prompt = build_prompt(prompt, resume_text, job_text)
        progress.update(task, description="Calling model route")
        raw, provider, model_used, trace, ranked = call_route(
            route_items,
            rendered_prompt,
            paid_fallback,
            openrouter_model,
            openrouter_api_key_env,
            refresh_openrouter_models,
            openai_model,
            gemini_model,
            timeout,
        )
        progress.update(task, description="Validating model output")
        data = extract_json(raw)
        require_fields(data)

    show_trace(trace)
    show_ranked_models(ranked)
    show_decisions(data)

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_file = output_dir / "tailored_resume.json"
    tex_file = output_dir / "tailored_resume.tex"
    if template:
        selected_template = template_path(template, template_dir)
    elif resume_path.suffix.lower() == ".tex" and preserve_tex_format:
        selected_template = derive_tex_template(resume_path, output_dir / "derived_template_from_resume.tex")
    else:
        selected_template = template_path("classic", template_dir)
    json_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tex_file.write_text(render_latex(data, selected_template), encoding="utf-8")
    outputs = [json_file, tex_file]
    if output_format in {OutputFormat.pdf, OutputFormat.both}:
        pdf_file = compile_pdf(tex_file, output_dir, latex_engine, require_pdf)
        if pdf_file:
            outputs.append(pdf_file)
    show_outputs(outputs)
    CONSOLE.print(f"[green]Done[/green] using [bold]{provider}[/bold] / [bold]{model_used}[/bold]")


@app.command("import-template")
def import_template(
    source: Path = typer.Argument(..., help="Source .tex or .pdf file."),
    name: str = typer.Option(..., "--name", help="Template name to create."),
    template_dir: Path = typer.Option(DEFAULT_TEMPLATE_DIR, "--template-dir", help="Template directory."),
) -> None:
    source = source.resolve()
    destination_dir = template_dir.resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{name}.tex"
    if source.suffix.lower() == ".tex":
        extract_tex_template(source, destination)
        CONSOLE.print(f"[green]Imported LaTeX template:[/green] {destination}")
    elif source.suffix.lower() == ".pdf":
        note = extract_pdf_template(source, destination)
        CONSOLE.print(f"[yellow]{note}[/yellow]")
        CONSOLE.print(f"[green]Created best-effort PDF-derived template:[/green] {destination}")
    else:
        raise typer.BadParameter("Template source must be a .tex or .pdf file.")


@app.command("templates")
def templates(template_dir: Path = typer.Option(DEFAULT_TEMPLATE_DIR, "--template-dir", help="Template directory.")) -> None:
    table = Table(title="Templates")
    table.add_column("Name", style="cyan")
    table.add_column("Path")
    for item in sorted(template_dir.glob("*.tex")):
        table.add_row(item.stem, str(item))
    CONSOLE.print(table)


@app.command("setup")
def setup(force: bool = typer.Option(False, "--force", help="Re-prompt even when keys are already configured.")) -> None:
    setup_wizard(Path(".env"), force)


def help_panel() -> None:
    table = Table(title="Commands")
    table.add_column("Command", style="cyan")
    table.add_column("What it does")
    table.add_row("tailor", "Tailor a resume to a pasted or file-based job description.")
    table.add_row("diagnose", "Check local inputs, template, provider route, and PDF tooling.")
    table.add_row("templates", "List available LaTeX templates.")
    table.add_row("import-template", "Import a .tex template or create a best-effort template from a PDF.")
    table.add_row("quit", "Exit the interactive shell.")
    CONSOLE.print(table)
    CONSOLE.print("[dim]Tip:[/dim] Run [bold]resume-tailor tailor --help[/bold] for every flag.")


def tailor_wizard() -> None:
    CONSOLE.print(Panel.fit("[bold cyan]Resume tailoring[/bold cyan]\nSupported resumes: .md, .txt, .tex, .pdf, .docx, .doc"))
    resume_default = os.environ.get("RESUME_TAILOR_RESUME", "samples/resume.md")
    resume_path = Path(Prompt.ask("Resume path", default=resume_default))
    try:
        job_text = read_job_from_cli()
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        CONSOLE.print(f"[red]Error:[/red] {exc}")
        return

    route_value = Prompt.ask("Route", default=os.environ.get("RESUME_TAILOR_ROUTE", DEFAULT_ROUTE))
    fallback_value = PaidFallback(
        Prompt.ask(
            "Paid fallback",
            choices=[item.value for item in PaidFallback],
            default=os.environ.get("RESUME_TAILOR_PAID_FALLBACK", PaidFallback.ask.value),
        )
    )
    output_value = OutputFormat(Prompt.ask("Output format", choices=[item.value for item in OutputFormat], default=OutputFormat.both.value))
    default_template = "" if resume_path.suffix.lower() == ".tex" else "classic"
    template_answer = Prompt.ask("Template (blank preserves .tex resume format when possible)", default=default_template)
    preserve_tex = True
    if resume_path.suffix.lower() == ".tex" and not template_answer:
        preserve_tex = Prompt.ask("Preserve .tex resume format?", choices=["yes", "no"], default="yes") == "yes"

    temp_job_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(job_text)
            temp_job_path = Path(handle.name)
        tailor(
            resume=resume_path,
            job=temp_job_path,
            prompt=DEFAULT_PROMPT,
            template=template_answer or None,
            template_dir=DEFAULT_TEMPLATE_DIR,
            output_dir=Path("output"),
            env=Path(".env"),
            route=route_value,
            paid_fallback=fallback_value,
            openrouter_api_key_env="OPENROUTER_API_KEY",
            openrouter_model=DEFAULT_OPENROUTER_MODEL,
            refresh_openrouter_models=False,
            show_route_flag=True,
            openai_model=DEFAULT_OPENAI_MODEL,
            gemini_model=DEFAULT_GEMINI_MODEL,
            timeout=int(os.environ.get("RESUME_TAILOR_TIMEOUT", "240")),
            latex_engine=None,
            output_format=output_value,
            preserve_tex_format=preserve_tex,
            no_pdf=False,
            require_pdf=False,
        )
    except (ProviderError, RouteBlocked, ValueError, RuntimeError, FileNotFoundError, typer.BadParameter) as exc:
        CONSOLE.print(f"[red]Error:[/red] {exc}")
    finally:
        if temp_job_path and temp_job_path.exists():
            temp_job_path.unlink()


@app.command("interactive")
def interactive() -> None:
    if not has_any_provider_key(Path(".env")):
        if not setup_wizard(Path(".env"), force=False):
            return
    CONSOLE.print(Panel.fit("[bold cyan]resume-tailor[/bold cyan]\nInteractive shell"))
    tailor_wizard()
    while True:
        choice = Prompt.ask(
            "\nWhat do you want to do?",
            choices=["tailor", "templates", "import-template", "help", "quit"],
            default="quit",
        )
        if choice == "quit":
            CONSOLE.print("[green]Bye.[/green]")
            return
        if choice == "help":
            help_panel()
            continue
        if choice == "templates":
            templates(DEFAULT_TEMPLATE_DIR)
            continue
        if choice == "import-template":
            source = Path(Prompt.ask("Path to .tex or .pdf template"))
            name = Prompt.ask("Template name")
            import_template(source, name, DEFAULT_TEMPLATE_DIR)
            continue

        tailor_wizard()


def main() -> None:
    commands = {"tailor", "diagnose", "import-template", "templates", "interactive", "setup"}
    argv = sys.argv[1:]
    if not argv:
        interactive()
        return
    if argv and argv[0] not in commands and argv[0] not in {"-h", "--help"}:
        argv = ["tailor", *argv]
    try:
        app(args=argv, standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc
    except click.exceptions.Exit as exc:
        raise SystemExit(exc.exit_code) from exc
    except (ProviderError, RouteBlocked, ValueError, RuntimeError, FileNotFoundError) as exc:
        ERROR_CONSOLE.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
