# Security

## Credential handling

| Credential | Where it lives | Protection |
|---|---|---|
| OAuth refresh token | `~/.anaplan_audit/tokens.db` | Encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256) |
| Fernet keyfile | `~/.anaplan_audit/token.key` | `0600` permissions, machine-local, generated on first use |
| Basic-auth username/password | Environment variables only (`ANAPLAN_AUDIT_BASIC_USERNAME` / `_PASSWORD`) | Never written to `settings.json` or logs |
| Certificate private key | Path you configure (`certPrivatePath`) | Stays on disk where you put it; optional passphrase supported |

`settings.json` intentionally contains **no secrets** — only IDs, names,
and feature flags. It is safe to commit an anonymized copy, but the
repository `.gitignore` excludes it by default because real files contain
tenant and model IDs you may not want public.

## If a credential leaks

- **OAuth refresh token or keyfile:** delete `~/.anaplan_audit/`, revoke
  the OAuth client's refresh tokens in Anaplan Administration, and
  re-register (`anaplan-audit register --client-id <ID>`).
- **Basic credentials:** rotate the Anaplan account password.
- **Certificate private key:** revoke the certificate with your CA and
  issue a replacement; update `certPrivatePath`.

## Network posture

All traffic is HTTPS to Anaplan-operated endpoints
(`auth.anaplan.com`, `api.anaplan.com`, `audit.anaplan.com`,
`api.cloudworks.anaplan.com`, `us1a.app.anaplan.com`). The tool makes no
other outbound calls and runs no listener.

## Data at rest

The SQLite database contains your tenant's audit events (user IDs,
emails, IP addresses, model activity). Treat it with the same
sensitivity as the Anaplan audit log itself: restrict filesystem
permissions, keep it off shared network drives, and include it in your
data-retention policy (`auditRetentionYears` / `modelHistory.retentionYears`
automate purging).

## Reporting a vulnerability

Open a private security advisory on the GitHub repository, or contact
the maintainer directly. Please do not open public issues for
exploitable problems.
