# Contributing

Thanks for helping improve the Beeper → Poke bridge. It's a small, focused
project — contributions that keep it simple and reliable are very welcome.

## Project layout

| File | What it does |
|---|---|
| `bridge.py` | Main process: Beeper listener, filters, debounce, single-instance lock, Telegram handoff. |
| `gatekeeper.py` | The LLM triage gate and its prompt. |
| `gatekeeper_eval.py` | Offline accuracy/latency eval for the gate. |
| `configure.py` | Interactive `.env` setup. |
| `watchdog.ps1` | Windows liveness watchdog. |
| `deploy/` | systemd / launchd / Windows supervisor configs. |

## Dev setup

```bash
git clone <your-fork> && cd beeper-poke-bridge
cp .env.example .env        # or: python configure.py
uv run --with-requirements requirements.txt python bridge.py --login
uv run --with-requirements requirements.txt python bridge.py
```

`uv` is recommended (no venv to manage). Plain `pip install -r requirements.txt`
in a venv works too. On Windows, keep the dependency cache out of `Downloads` or
add a Defender exclusion — see `deploy/windows/README.md`.

## Changing the gate

Most behaviour lives in the `gatekeeper.py` system prompt (what earns an
interruption). If you touch it, run the eval and include before/after numbers in
your PR:

```bash
uv run --with-requirements requirements.txt python gatekeeper_eval.py
# compare models: EVAL_MODELS="gpt-4o-mini,gpt-4.1-nano" ...python gatekeeper_eval.py
```

Add labelled cases to `CASES` in `gatekeeper_eval.py` when you find a message
the gate gets wrong — that's the most valuable contribution.

## Guidelines

- Keep it dependency-light and cross-platform (Windows/macOS/Linux).
- Don't break the safety invariants: the bridge only ever messages the Poke bot,
  never third parties; the gate defaults to silence; gateway errors fail **open
  for DMs, closed for groups**.
- Preserve the single-instance lock and heartbeat — supervisors depend on them.
- **Never commit secrets.** `.env`, `*.session`, logs, and runtime state are
  gitignored; keep it that way. Don't paste real tokens into issues or PRs.
- Match the existing style: standard library first, small pure functions,
  comments only where the *why* isn't obvious.

## Pull requests

1. Branch off `main`.
2. Keep PRs focused — one concern each.
3. Describe what changed and how you tested it. For gate changes, include eval
   numbers.
4. Make sure `python -c "import ast,sys; ast.parse(open('bridge.py').read())"`
   and a fresh `--login`-less import still work.

## Reporting bugs

Open an issue with: your OS, how you run it (uv / venv, supervisor), the
relevant `bridge.log` lines (redact tokens), and what you expected vs. saw.
