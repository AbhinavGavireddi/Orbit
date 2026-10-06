# Contributing

Thanks for helping improve Orbit. This project is a local-first desktop agent prototype, so contributions should preserve user consent, privacy and recoverability before adding capability.

## Ground Rules

- Do not commit secrets, API keys, `.env` files, provider responses with credentials, personal screenshots or raw audio.
- Keep desktop-control changes fail-closed: uncertain state should stop, ask or require explicit approval rather than guessing.
- Avoid broad rewrites when a small, testable change solves the issue.
- Keep provider-specific code behind clear boundaries where practical.
- Document new user-facing risks, permissions or data flows in the relevant docs.

## Development Flow

1. Open an issue or discussion for non-trivial behavior changes.
2. Create a focused branch for the work.
3. Add or update tests for behavior changes.
4. Run the relevant checks from `docs/DEVELOPMENT.md`.
5. Open a pull request with the user impact, safety/privacy impact and verification performed.

## Pull Request Checklist

- The change avoids committing private data or generated artifacts.
- Tests or manual verification are included for the changed behavior.
- Safety gates, approval flows and cancellation behavior are not weakened.
- Documentation is updated when setup, permissions, data handling or public APIs change.
- The PR description calls out any remaining limitations honestly.

## Reporting Security Issues

Please follow `SECURITY.md` for vulnerability reports instead of filing public issues with exploit details.
