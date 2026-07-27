# Security and privacy

## Reporting a vulnerability

Do not open a public issue containing credentials, device keys, personal data, server logs, or exploit details. Use GitHub's private vulnerability reporting for this repository when available.

## Trust boundaries

- Ordinary accounts are isolated from one another by user and device ownership.
- A device key grants control of that device and must be handled like a password.
- Administrators are trusted operators. The optional credential vault intentionally gives an authenticated administrator access to encrypted account and device credentials.
- The relay operator can access the server database and encryption key. This project does not protect users from a malicious server operator.

## Deployment requirements

1. Generate a unique administrator password hash, Fernet key, and optional legacy API tokens for every deployment.
2. Keep `/etc/funf-relay.env`, the SQLite database, backups, logs, certificates, and signing keys outside the repository with restrictive filesystem permissions.
3. Expose only HTTPS/WSS through a reverse proxy. Keep the application port bound to `127.0.0.1`.
4. Use a publicly trusted TLS certificate. Mobile clients must never disable certificate validation.
5. Prefer the `Authorization: Bearer` device authentication supported by current clients. Query-string device tokens remain server-side only for compatibility with older clients and may appear in proxy logs.
6. Disable or redact query-string access logs, rotate any exposed key immediately, and revoke affected sessions.
7. Back up the database and vault key separately. Losing the vault key makes encrypted credentials unrecoverable.

## Repository hygiene

The repository intentionally excludes environment files, databases, logs, packet captures, private documentation, certificates, signing keys, APKs, screenshots, IDE state, and test runtime data. `server/.env.example` contains placeholders only.

Before every public release, inspect both the working tree and Git history for secrets. Never rely on `.gitignore` after a secret has already been committed; purge it from history and rotate it.
