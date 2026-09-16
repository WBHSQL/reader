# Security Policy

Do not open a public issue containing cookies, authorization state, private article text, personal reading history, or local filesystem data.

Please use GitHub's private security reporting channel when available.

High-priority reports include:

- credential or cookie leakage;
- unsafe default network exposure;
- persistence of raw article bodies where only ephemeral processing is intended;
- source-verification bypasses that accept summaries or mismatched articles as originals;
- path traversal or arbitrary local-file access through the HTTP interface.

Sanitize private content before sharing reproduction steps.
