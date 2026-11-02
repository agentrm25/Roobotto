# Contributing to Roobotto

Roobotto drives live-show production cues, so the rules are strict. Read
[AGENTS.md](AGENTS.md) and [docs/implementation-plan.md](docs/implementation-plan.md)
(Sections 3–5 hold the contracts) first.

## Workflow

1. One task card per branch and per pull request. Keep PRs small.
2. Restate the card's acceptance tests and write them first.
3. Before opening a PR, all of these must pass:
   ```
   uv run ruff check
   uv run ruff format --check
   uv run pyright
   uv run pytest
   ```
4. Update docs and `docs/evidence-register.md` for any research or hardware claim.
5. In the PR, list what was **verified** and what was **assumed**. Never claim hardware
   testing without a bench log in `captures/`.
6. If the work needs anything done outside the coding environment (equipment setup, wiring,
   bench tests, captures, recordings, annotation), write a Tech Order first using the
   template in [docs/tech-orders/](docs/tech-orders/README.md) and link it from the PR.
   Acceptance checks that depend on it stay open until its sign-off is complete.

## Ask the owner first

- Adding a dependency (record its license in `THIRD_PARTY.md`)
- Changing a schema, contract, or invariant (needs an ADR)
- Touching a real-device adapter
- Anything that changes what reaches hardware

## Never commit

- Audio files, recordings, or bench captures
- Lyrics or private show data (these stay in `local/`)
- Credentials

CI rejects audio files and anything under `local/`, `recordings/`, or `captures/`.

## License

Contributions are licensed under Apache-2.0 (see [LICENSE](LICENSE)).
