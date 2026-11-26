# Security Policy

## Supported Versions

| Version / Branch | Security Updates |
| --- | --- |
| `0.1.x` (latest stable release) | Supported |
| `main` (development branch) | Supported |
| `< 0.1.0` | Unsupported |

## Reporting a Vulnerability

**Please do not report security vulnerabilities in public GitHub issues or pull requests.**

Report suspected vulnerabilities privately through GitHub Security Advisories:

- **Private Advisory Form:** [https://github.com/jkuzel02/RCAnnon/security/advisories/new](https://github.com/jkuzel02/RCAnnon/security/advisories/new)

Include as much context as possible to help maintainers reproduce and triage quickly:

- Affected component, release tag, or commit SHA
- Configuration or deployment topology (Docker Compose vs. management Kubernetes cluster)
- Proof of concept or reproduction steps
- Potential impact (credential exposure, container escape, RBAC escalation, or prompt injection bypass)

Maintainers will acknowledge receipt, assess severity, and coordinate a fix before public disclosure.

## Security & Isolation Model

RCAnnon is built to inspect failing Kubernetes clusters without exposing credentials, mutating cluster state, or tying its own availability to the failing cluster:

- **Hardened Multi-Arch Containers & Provenance:** Prebuilt `linux/amd64` and `linux/arm64` API images run as non-root `UID=10001` with `read_only: true`, dropped Linux capabilities (`ALL`), `no-new-privileges:true`, SBOMs, and verifiable SLSA provenance:

```sh
gh attestation verify oci://ghcr.io/jkuzel02/rcannon:latest --owner jkuzel02
```

- **Strict Network & Secret Boundaries:** LibreChat (`v0.8.9`, `UID=1000`, `read_only: true`, `cap_drop: [ALL]`, public registration and social login disabled, native tools/uploads/web-search turned off) binds only `127.0.0.1:3080` by default and holds zero cluster kubeconfigs, LLM API keys, or operator tokens. MongoDB (`8.0`) sits exclusively on an internal `ui_db` Docker network with zero published host ports.
- **Idempotent `stdin` Admin Bootstrap:** Initial administrator creation runs through an opt-in one-shot `--profile bootstrap` container piping the secret password solely over `stdin` to LibreChat's stock `npm run create-user` CLI—never via CLI arguments, stdout, logs, or `.env`—and fails closed on conflicting database state or weak credentials.
- **Target Cluster Guardrails & Redaction:** `TargetSelector` enforces cluster-alias and namespace allowlists, restricts Kubernetes API access to read-only `get`/`list` verbs (never `Secret` values, `pod exec`, or mutations), rejects kubeconfigs containing `exec` plugins, `auth-provider` blocks, or `insecure-skip-tls-verify: true`, and scrubs credentials before storage, inference, and SSE rendering.

For deeper contract details, see [docs/architecture.md](./docs/architecture.md), [docs/container-image.md](./docs/container-image.md), and [infrastructure/compose/secrets/README.md](./infrastructure/compose/secrets/README.md).

## Operator Responsibilities

- Keep the UI bound to loopback (`127.0.0.1:3080`) on local workstations, or terminate TLS cleanly behind an independently managed reverse proxy when exposing remote access.
- Restrict host secret file ownership and permissions (`UID=10001` for `api` secrets, `UID=1000` for LibreChat secrets, and Kubernetes `Secret` RBAC when running in a management cluster).
- Deploy RCAnnon outside the target cluster's failure domain (standalone host or separate management Kubernetes cluster) and grant only the minimum read-only namespace RBAC needed (`infrastructure/k8s/rbac/rcannon-readonly.yaml`).
