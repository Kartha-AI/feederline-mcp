# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/Kartha-AI/feederline-mcp/security/advisories/new) for this
repository. Do not open a public issue.

Include the affected version, a description, and steps to reproduce. We aim to acknowledge reports within three
business days.

## Scope notes

- The local stack binds to `127.0.0.1` and uses a random bearer token (npm path) or the static `dev-token` (from-source
  path, development only). Don't expose either to a network without TLS and real authentication.
- The server's database role is read-only on the network model and telemetry; findings that let a tool call write to
  the model, read raw telemetry, cross region (RLS) boundaries, or inject DSS commands are in scope.
- The local stack is for public data only. Real utility models are CEII and must not be loaded into it.
