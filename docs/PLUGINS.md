# Plugins

A siwah plugin is a **declarative bundle** of harness data. It can add skills,
slash commands, subagents, hooks, and MCP servers. Siwah never imports Python
code from a plugin, so installing a plugin does not add executable code to the
agent process.

Hooks are the exception to "no code": a hook is a command that runs when its
event fires. Install plugins only from sources you trust.

## Format

```text
my-plugin/
├─ plugin.toml
├─ skills/
│  └─ review/
│     └─ SKILL.md
├─ commands/
│  └─ review.md
├─ subagents/
│  └─ reviewer.md
├─ hooks/
│  └─ format.json
└─ mcp.json
```

`plugin.toml`:

```toml
name = "my-plugin"
version = "0.1.0"
description = "Project review helpers"
author = "Your Name"
license = "MIT"
homepage = "https://example.com/my-plugin"
```

`name` must be lowercase letters, digits, and hyphens. All other files are
optional. Limits: 5,000 files and 20 MB per plugin. Symlinks and zip entries
that escape the plugin directory are rejected.

Each component uses the same format as the corresponding harness directory:

- `skills/<name>/SKILL.md` — agentskills.io-style skill;
- `commands/*.md` — slash command with optional frontmatter and `$ARGUMENTS`;
- `subagents/*.md` — subagent spec with frontmatter;
- `hooks/*.json` — the JSON hook contract;
- `mcp.json` — a `{ "servers": { ... } }` object using the normal MCP schema.

## Install and manage

```bash
siwah plugin install ./my-plugin
siwah plugin install ./my-plugin.zip
siwah plugin list
siwah plugin info my-plugin
siwah plugin disable my-plugin
siwah plugin enable my-plugin
siwah plugin remove my-plugin
```

`install` copies the bundle to `~/.siwah/plugins/<name>/`. `remove` asks for
confirmation unless you pass `--yes`. `disable` leaves the files in place and
makes the loaders ignore them.

The loaders read plugins at session start. Start a new session (or restart the
daemon) after installing, enabling, or disabling a plugin. The TUI command
palette reloads on demand, but a pinned system prompt may not mention a new
skill until the next session.

## Precedence

- Project files (`.siwah/skills`, `.siwah/commands`, `.siwah/subagents`) win.
- Enabled plugins come next.
- The global harness in `~/.siwah/harness/` is the fallback.

For MCP servers, your own `~/.siwah/harness/mcp.json` wins over a plugin with the
same server name. A plugin-provided server cannot be deleted with
`siwah mcp remove`; disable or remove the plugin instead.

## Example: Context7 bundle

`plugin.toml`:

```toml
name = "context7"
version = "0.1.0"
description = "Context7 documentation lookup"
```

`mcp.json`:

```json
{
  "servers": {
    "context7": {
      "transport": "http",
      "url": "https://mcp.context7.com/mcp/oauth",
      "auth": "oauth",
      "scopes": ["profile", "email"],
      "description": "Context7 library docs"
    }
  }
}
```

Install and authorize:

```bash
siwah plugin install ./context7
siwah mcp auth context7
siwah mcp test context7
```

The `mcp auth` flow is generic OAuth discovery, not Context7-specific. For a
headless machine use `siwah mcp auth context7 --device`; for a remote terminal
use `--no-browser` and open the printed URL yourself.

## Security

- A plugin is data, but hooks execute commands and MCP servers receive tool
  calls. Install only bundles you trust.
- Plugin MCP servers use the same permission rules as user-configured servers.
- A plugin cannot edit the kernel, evaluation fixtures, or manifest.
- Do not put API keys or OAuth tokens in `plugin.toml` or `mcp.json`. Use the
  OAuth flow, an environment variable through `client_secret_env`, or the normal
  MCP headers with the understanding that headers are stored on disk.
- Removing a plugin deletes only `~/.siwah/plugins/<name>/`; it does not delete
  OAuth state. Use `siwah mcp logout <server>` for that.
