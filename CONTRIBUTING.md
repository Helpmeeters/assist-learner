# Contributing to Helpmeet: Assist Learner

Thanks for helping. Bug reports, wording fixes, and pull requests are all welcome.

## Reporting bugs

Open an [issue](https://github.com/Helpmeeters/assist-learner/issues/new/choose) using the bug report form. The most useful things to include are:

- Your Home Assistant version, and whether it's a beta.
- Which LLM conversation agent you use.
- A diagnostics download from the Assist Learner integration page. Utterances and entity IDs are redacted.
- The debug log lines for the command in question (see **Debug logging** in the [README](README.md#debug-logging)). These are not redacted, so trim anything you don't want to share.

If capture has paused with a **stopped learning** repair, that almost always means a Home Assistant update changed the conversation internals this integration reads. Please report it with the exact Home Assistant version.

## Development setup

You need Python 3.14 and [uv](https://docs.astral.sh/uv/). A devcontainer is also included.

```bash
uv venv -p 3.14
uv pip install pytest-homeassistant-custom-component 'hassil==3.12.1' 'home-assistant-intents==2026.8.28' 'gazetteer-matcher==1.1.0'
.venv/bin/python -m pytest -q
```

The end-to-end tests drive a fake LLM agent through the real chat log, Assist tools, and intents, then check the exported sentence against the real default agent. That includes confirming that a sentence learned in the kitchen turns on only the bedroom lamp when said in the bedroom.

## Pull requests

- Keep changes focused. One fix or feature per pull request.
- Add or update tests for any behavior change, and make sure `pytest` passes.
- Run `ruff check` and `ruff format` (settings are in `pyproject.toml`).
- Code that touches conversation internals belongs in `context_adapter.py`, behind its version check, so a core change pauses capture instead of breaking it.
- If you change `strings.json`, copy the same change into `translations/en.json`.
- CI runs the tests against Home Assistant stable and beta, plus hassfest and the HACS validation action. All of them need to pass.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
