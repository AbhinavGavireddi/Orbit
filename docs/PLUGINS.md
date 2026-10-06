# Plugins (MCP + skills)

Orbit loads MCP connectors from plugins config. Skill roots come from `ORBIT_SKILL_ROOTS` only. Task is still the only caller of the world. A write waits for Confirm. There is no auto-approve, no host shell, and no MCP server without an entry in plugins config.

## Config files

- `config/orbit.plugins.yaml` — local MCP list (empty `mcp_servers` by default)
- `config/orbit.plugins.example.yaml` — copy-paste shapes
- Env: `ORBIT_PLUGINS_CONFIG` (default `config/orbit.plugins.yaml`)
- Env: `ORBIT_SKILL_ROOTS` (default `./skills,home`) — the only skill-root setting; plugins YAML does not list roots

`home` expands to the usual personal skill directories (`~/.codex/skills`, `~/.claude/skills`, `~/.agents/skills`, and the Orbit learned-skills folder on macOS).

## Add an MCP server (3 steps)

1. Copy the example entry shape from `config/orbit.plugins.example.yaml` into `config/orbit.plugins.yaml` under `mcp_servers`.
2. Set either an argv `command` list (or `command` + `args`) **or** a `url`. Put secrets in `.env` / compose (for URL auth: `ORBIT_MCP_TOKEN` or `Authorization`), not in the YAML if you can avoid it. Never use a shell string; basename paths like `/bin/bash` and `env bash` are refused.
3. Restart Task. Call `mcp_read` / `mcp_call` with that server name. An unknown name fails closed; `mcp_call` still needs Confirm.

## Add a skill (3 steps)

1. Create `skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`) and short markdown steps.
2. Keep `ORBIT_SKILL_ROOTS` as `./skills,home` (or add another root in `.env` / compose).
3. Restart Voice / Automation so they re-index. The skill is a procedure window, not a second router; world changes still go through Task.

## Compose / Docker

The image copies `config/` and `skills/` (both must be un-ignored in `.dockerignore`). Compose passes `ORBIT_PLUGINS_CONFIG` into Task and defaults `ORBIT_SKILL_ROOTS` to `./skills,home` for Voice and Automation.

## Refuse

- Host shell / `shell: true` / spawning `bash`, `/bin/bash`, or `env bash` as the MCP command
- Auto-approve of `mcp_call`
- MCP use when the server name is missing from config
