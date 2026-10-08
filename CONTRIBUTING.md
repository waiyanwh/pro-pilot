# Contributing

Use Python 3.13+ and uv. Install development dependencies with `uv sync --all-extras`.
Run the offline checks in README.md for changes you make.

Read the relevant specs before changing behavior. Update the spec in the same
change; record architectural tradeoffs in docs/decisions.md. Keep patches small.

Tests must use synthetic SDK fixtures and local HTTP/WebSocket servers. Do not
capture or commit real credentials, account details, application logs or deployment
configuration. Never add mutating tools, Kubernetes Secret/ConfigMap reads or a
credential fallback that grants a spoke more access.

Open an issue with reproducible steps and redacted evidence for ordinary bugs.
Use SECURITY.md for suspected vulnerabilities. Contributions are made under the
repository's license.
