## What this changes

Brief description of the change and the problem it solves.

## How I tested it

- [ ] `python bridge.py --login` / startup still works
- [ ] Ran the bridge against a live Beeper for a few minutes
- [ ] For gate changes: ran `gatekeeper_eval.py` (before/after numbers below)

```
eval output here (if applicable)
```

## Checklist

- [ ] One focused concern
- [ ] No secrets committed (`.env`, `*.session`, tokens in code/logs)
- [ ] Cross-platform friendly (no new hard-coded paths)
- [ ] Preserves safety invariants (silent by default; only messages the Poke bot;
      fails open for DMs / closed for groups)
