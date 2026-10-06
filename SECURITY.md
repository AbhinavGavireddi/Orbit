# Security Policy

Orbit controls local desktop capabilities and may connect to model providers, so security issues should be handled carefully.

## Supported Versions

This repository is an early prototype. Unless releases are published, security fixes target the default branch only.

## Reporting a Vulnerability

- Do not open a public issue with exploit details, credentials, screenshots, raw audio or private logs.
- Prefer a GitHub private vulnerability report if the repository has Security Advisories enabled.
- If private advisories are unavailable, contact the maintainers through the repository owner profile and request a private reporting channel without disclosing details publicly.

Please include:

- Affected commit, release or configuration.
- Steps to reproduce using dummy data where possible.
- Expected and observed impact.
- Whether any credentials, local files or third-party accounts may be affected.

## Scope

High-priority issues include:

- Secret leakage through logs, artifacts, images, screenshots or provider payloads.
- Unauthorized desktop actions, approval bypasses or stale action replay.
- Remote command execution or shell/script execution paths.
- Cross-session task confusion, device-token misuse or fence/cancellation bypasses.
- Persistent storage of data that documentation says is transient.

Out of scope for private security handling:

- General product suggestions.
- Model quality complaints without a security impact.
- Vulnerabilities requiring a compromised local machine with unrestricted user privileges and no additional project-specific impact.

## Secret Handling

Never share real provider keys, device tokens or service tokens in issues, pull requests, logs or chat. Use `.env.example` for configuration shape and keep `.env` local.
