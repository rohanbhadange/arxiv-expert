# Security policy

## Supported versions

Security fixes are applied to the latest `0.1.x` release and the default branch.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed credential.
Use GitHub's private vulnerability reporting from the repository's **Security**
tab. Include the affected version, reproduction steps, impact, and any proposed
mitigation. Do not include a working API key or private chat content.

## Local security model

- The OpenAI API key is stored in the platform's per-user configuration directory.
- On POSIX systems, configuration, data, and log directories are mode `0700`; the
  API-key file and SQLite database are mode `0600`.
- The chat server binds to `127.0.0.1` and is not intended for remote exposure.
- Public arXiv PDFs and chat prompts are sent to OpenAI for indexing and answers.
- The repository never needs an API key in source code, `.env` files, test fixtures,
  CI variables, or issue reports.

If you deliberately expose the service through a proxy, add authentication, TLS,
request limits, and an explicit data-retention policy first.
