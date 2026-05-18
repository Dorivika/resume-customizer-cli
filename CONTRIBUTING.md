# Contributing

This project is intentionally small and CLI-focused. Keep changes narrow, testable, and documented.

## Local setup

```bash
python -m pip install -e .
resume-tailor --help
python -m unittest discover -s tests
```

## Pull requests

- Describe the user-facing behavior being changed.
- Include a short manual test command when the change affects CLI behavior.
- Do not commit local provider keys or generated resume output.
