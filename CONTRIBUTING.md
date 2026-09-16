# Contributing

Contributions are welcome.

Before opening a pull request:

1. Do not commit cookies, auth state, private reading history, paywalled text, or downloaded article bodies.
2. Keep networked source adapters opt-in and local-first defaults safe.
3. Add tests when changing source resolution or originality verification.
4. Run `python -m pip install -r requirements.txt` and `python -m unittest discover -s tests -v`.
5. Use synthetic fixtures rather than copyrighted article snapshots when possible.

For security-sensitive findings, follow `SECURITY.md`.
