# Security policy

## Supported versions

Security fixes target the latest 1.0.x release. Pre-1.0 builds are not supported.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's security
advisory flow:

<https://github.com/zidanmubarak/siwah/security/advisories/new>

Do **not** open a public issue for a vulnerability, and do not include API keys,
tokens, private source code, or personal data in a report.

If the advisory form is unavailable, contact the maintainer through the GitHub
profile <https://github.com/zidanmubarak> and ask for a private channel. Do not
send secrets in the first message.

## What to include

- siwah version (`siwah --version`) and install method;
- operating system, terminal, and shell;
- the affected command or endpoint;
- a minimal reproduction with placeholders instead of secrets;
- the impact you believe is possible;
- whether the issue requires a malicious repository, a malicious MCP server, a
  compromised provider, or only local access;
- any suggested fix or mitigation.

## What not to do

- Do not test against systems or data you do not own or have permission to test.
- Do not run destructive commands, exfiltrate data, or attempt denial of service.
- Do not publish details before a fix or coordinated disclosure date.
- Do not include real credentials or private code in screenshots or logs.

## Scope

In scope:

- the siwah source code in this repository;
- the daemon HTTP + SSE API and ACP bridge;
- the TUI, launcher, supervisor, and kernel enforcement;
- the permission rules, path guard, sandbox integration, and update flow;
- evaluation and evolution gates;
- documentation that could cause a security-relevant misconfiguration.

Out of scope:

- vulnerabilities in a provider, model, editor, terminal, or third-party
  dependency (report them upstream; tell us if siwah's integration makes it
  worse);
- a user deliberately enabling `yolo`, disabling the path guard, or exposing the
  daemon to a network;
- a malicious `bash` command that escapes the workspace when no sandbox is
  enabled, unless the escape bypasses a control that claims to prevent it;
- social engineering or physical access;
- missing hardening that is already documented as a limitation.

The technical threat model and the limits of the path guard are documented in
[docs/SECURITY.md](docs/SECURITY.md).

## Response

This is a small project. Reports are handled on a best-effort basis, without a
guaranteed response or fix deadline. We will acknowledge a valid report when
possible, discuss a fix, and coordinate disclosure. Please give us reasonable
time before publishing.

## Secrets

If you accidentally expose a credential in an issue, pull request, log, or
screenshot:

1. rotate the credential immediately;
2. remove the public content if you can;
3. tell the maintainers so the exposure can be documented and the credential
   revoked.

siwah stores provider keys in `~/.siwah/auth.json` with mode 600 and keeps the
daemon token in `~/.siwah/runtime.json` with mode 600. Neither file should be
committed or shared.
