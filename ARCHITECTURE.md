# Architecture

Accepted 2026-10-06. The diagram in [README.md](README.md) is the same picture. This file is the rule.

## Fast path

The face, `WS /v1/voice`, Redis, Confirm, Schedule, the epoch, and the ping stay as they are. They take no new library. Confirm stays in the task process.

## One grant

Task is the only caller of the world. The worker may ask for an action. It may not perform one.

A read is an observation. It has an action id. A write waits for Confirm. Spoken yes is conversation. Talk, Confirm, Schedule, and Dismiss are different controls.

The room, the page, and an MCP tool share that door. A new world registers an adapter. The store does not grow a branch for it.

## Page

The face tab is not the work browser. The work browser is an empty Chromium. `ORBIT_BROWSER_HOSTS` is the allowlist. An empty list refuses every page.

A goal with one https address asks for a page read, and only when that list is set. A click, a type, or a key is `browser_act`. The card shows the URL, the verb, and the target. After a crash, the next attempt reads the page again. It does not replay a click.

Playwright loads on the first page call. It is not installed in the image yet. The fast path does not import it.

## MCP

`mcp_read` and `mcp_call` are the import door. A server is injected. None is configured. The door does not start a process and does not grow a shell. A write needs Confirm.

## Refuse

A host shell, auto-approve, chat-channel presence, a second voice runtime, a checkpoint that replays a click, and a memory store that replaces Redis on the talk path stay out. Pipecat, Letta, Browser Use, Stagehand, Hermes, OpenClaw, and DeepSeek Harness stay out.
