# Resume Tailor

`resume-tailor` is a Typer + Rich CLI that tailors a real resume to a pasted or file-based job description, prints model routing and keep/drop/rewrite decisions, renders a LaTeX resume, and compiles a PDF when a LaTeX engine is installed.

Resume inputs can be `.md`, `.txt`, `.tex`, `.pdf`, `.docx`, or best-effort legacy `.doc`.

The default route is designed for a low-cost/free-first workflow:

```text
openrouter-free -> gemini -> openai
```

Users must provide their own provider keys. The app never embeds shared API keys.

## Install

```bash
python -m pip install -e .
```

Then:

```bash
resume-tailor --help
resume-tailor tailor --help
resume-tailor diagnose --help
```

`--help` is intentionally non-interactive, like most CLIs: it prints usage and exits. To stay inside the app, run:

```bash
resume-tailor
```

or:

```bash
resume-tailor interactive
```

On first launch, `resume-tailor` checks for provider keys in your environment and local `.env`. If none are found, it starts setup first, then moves directly into resume tailoring.

Run setup manually:

```bash
resume-tailor setup
resume-tailor setup --force
```

OpenRouter, Gemini, and OpenAI keys are optional individually. At least one provider key is required for live tailoring.

## Fast Start

Paste the job description into the CLI:

```bash
resume-tailor tailor --resume samples/resume.md
```

Finish the paste with:

```text
.done
```

Inside interactive mode, paste the full job description when prompted. Do not paste it into a yes/no or option prompt. The app now asks for the full JD as multiline text and waits until `.done`.

To load the JD from a file during paste mode, type this as the first line:

```text
/file path/to/job-description.txt
```

File-based run:

```bash
resume-tailor tailor --resume samples/resume.md --job samples/job_description.txt
```

Before calling a provider, check that local files, templates, route settings, and PDF tooling are ready:

```bash
resume-tailor diagnose --resume samples/resume.md --job samples/job_description.txt
```

Skip PDF compilation:

```bash
resume-tailor tailor --resume samples/resume.md --job samples/job_description.txt --no-pdf
```

Choose output format:

```bash
resume-tailor tailor --resume samples/resume.md --job samples/job_description.txt --output-format tex
resume-tailor tailor --resume samples/resume.md --job samples/job_description.txt --output-format pdf
resume-tailor tailor --resume samples/resume.md --job samples/job_description.txt --output-format both
```

## Free Tier With OpenRouter

OpenRouter free routing uses a user-supplied `OPENROUTER_API_KEY`.

```bash
export OPENROUTER_API_KEY="sk-or-..."
resume-tailor tailor --resume samples/resume.md --job samples/job_description.txt --route openrouter-free
```

PowerShell:

```powershell
$env:OPENROUTER_API_KEY = "sk-or-..."
resume-tailor tailor --resume .\samples\resume.md --job .\samples\job_description.txt --route openrouter-free
```

By default, `--openrouter-model auto-free` fetches OpenRouter model metadata, filters for free text models, ranks the strongest candidates, and uses the top model. The CLI prints the selected model and the actual model returned by OpenRouter.

OpenRouter free models are rate-limited and may be unavailable under load. This is useful for a free tier, but it should not be treated as guaranteed production capacity.

References:

- [OpenRouter Models API](https://openrouter.ai/docs/guides/overview/models)
- [OpenRouter routing](https://openrouter.ai/docs/model-routing)
- [OpenRouter rate limits](https://openrouter.ai/docs/api/reference/limits)
- [OpenRouter requests and JSON mode](https://openrouter.ai/docs/requests)

## Route Flags

Control provider fallback order with `--route`:

```bash
resume-tailor tailor --resume ./resume.md --route openrouter-free,gemini,openai
resume-tailor tailor --resume ./resume.md --route gemini,openrouter-free,openai
resume-tailor tailor --resume ./resume.md --route openai
```

Valid route providers:

- `openrouter-free`
- `gemini`
- `openai`

Control paid fallback behavior:

```bash
resume-tailor tailor --resume ./resume.md --paid-fallback ask
resume-tailor tailor --resume ./resume.md --paid-fallback auto
resume-tailor tailor --resume ./resume.md --paid-fallback never
```

Modes:

| Mode | Behavior |
| --- | --- |
| `ask` | Prompt before falling through to a paid provider. Default. |
| `auto` | Automatically fall through to paid providers when earlier providers fail. |
| `never` | Stop before a paid provider is attempted. |

Print the route before running:

```bash
resume-tailor tailor --resume ./resume.md --show-route
```

Run a preflight check without calling a model:

```bash
resume-tailor diagnose --resume ./resume.md --job ./job.txt --route openrouter-free,gemini
resume-tailor diagnose --resume ./resume.md --job ./job.txt --strict
resume-tailor diagnose --resume ./resume.md --job ./job.txt --require-pdf --strict
```

`diagnose` verifies readable input files, template resolution, output directory readiness, available LaTeX engines, provider route syntax, and which provider keys are configured.

## Environment

Copy the example:

```bash
cp .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

Important values:

```text
OPENROUTER_API_KEY=
OPENROUTER_MODEL=auto-free
OPENROUTER_APP_TITLE=Resume Tailor
OPENROUTER_REFERER=

RESUME_TAILOR_ROUTE=openrouter-free,gemini,openai
RESUME_TAILOR_PAID_FALLBACK=ask
RESUME_TAILOR_RESUME=./samples/resume.md

GEMINI_API_KEY=
GOOGLE_API_KEY=
GEMINI_MODEL=gemini-2.5-flash

OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.3-codex
OPENAI_ORG_ID=
OPENAI_PROJECT_ID=
```

`.env` is ignored by Git. Do not commit real keys.

Provider keys are saved only if you enter them during setup. Empty setup answers are skipped.

## Security

`resume-tailor` does not implement ChatGPT OAuth, browser-cookie scraping, Codex token reuse, or embedded shared keys. Those approaches are unsafe for an open-source CLI because source code and distributed artifacts are visible to users.

What the app does instead:

- Reads provider keys from environment variables or local `.env`.
- Never writes provider keys to output files.
- Uses a user-owned OpenRouter key for free model access.
- Uses OpenAI and Gemini keys only if those providers are included in the route and fallback policy allows them.
- Stores only OpenRouter model metadata in a local cache outside tracked source.

## Prompt

The tailoring prompt lives in `prompt.md`.

Use a custom prompt:

```bash
resume-tailor tailor --resume samples/resume.md --job samples/job_description.txt --prompt ./prompt.md
```

Prompt placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{{RESUME}}` | Source resume text. |
| `{{JOB_DESCRIPTION}}` | Job description text. |

## Resume Input Formats

Supported resume file types:

| Format | Support |
| --- | --- |
| `.md`, `.txt`, `.tex` | Read directly as text. |
| `.pdf` | Extracts text with `pdftotext` when installed, or optional `pypdf`. |
| `.docx` | Extracts text directly from the Word document XML. |
| `.doc` | Best effort through `antiword`, `catdoc`, or macOS `textutil` if installed. |

For PDF extraction fallback:

```bash
python -m pip install -e ".[pdf]"
```

Legacy `.doc` is an old binary format. If extraction fails, convert it to `.docx` or PDF first.

## Format Preservation For LaTeX Resumes

If your source resume is a `.tex` file and you do not pass `--template`, the app derives the output template from that `.tex` file:

- If the `.tex` file already has Resume Tailor placeholders, it renders into them.
- If it does not have placeholders, the app preserves the preamble/packages/layout and replaces the document body with the tailored resume sections.
- The original `.tex` file is not overwritten.

Use an explicit template to override this behavior:

```bash
resume-tailor tailor --resume ./resume.tex --job ./job.txt --template classic
```

Disable preservation:

```bash
resume-tailor tailor --resume ./resume.tex --job ./job.txt --no-preserve-tex-format
```

Exact format preservation is only available for `.tex` inputs. PDF, DOCX, and DOC inputs are text-extracted and rendered through the selected template.

## Templates

List templates:

```bash
resume-tailor templates
```

Use a named template:

```bash
resume-tailor tailor --resume samples/resume.md --job samples/job_description.txt --template classic
```

Import from LaTeX:

```bash
resume-tailor import-template ./my-resume.tex --name personal
resume-tailor tailor --resume samples/resume.md --job samples/job_description.txt --template personal
```

Import from PDF:

```bash
resume-tailor import-template ./my-resume.pdf --name pdf-template
```

PDF files do not contain recoverable LaTeX source. The PDF importer extracts text when `pdftotext` or optional `pypdf` is available, stores that extracted text as comments, and creates a usable best-effort template based on the default layout.

Optional PDF text dependency:

```bash
python -m pip install -e ".[pdf]"
```

Template placeholders:

| Placeholder | Meaning |
| --- | --- |
| `{{NAME}}` | Candidate name. |
| `{{HEADLINE}}` | Tailored headline. |
| `{{CONTACT}}` | Contact line. |
| `{{RESUME_BODY}}` | Rendered resume sections. |
| `{{KEYWORD_REPORT_COMMENT}}` | Keyword report as LaTeX comments. |

## Output

By default, output goes to `output/`:

```text
output/tailored_resume.json
output/tailored_resume.tex
output/tailored_resume.pdf
```

If no LaTeX engine is installed, JSON and LaTeX are still written and the CLI prints a warning. Use `--require-pdf` to fail when PDF compilation is unavailable.

Supported LaTeX engines:

- `tectonic`
- `latexmk`
- `pdflatex`
- `xelatex`
- `lualatex`
