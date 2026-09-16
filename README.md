# Reader

A provider-agnostic full-text resolver and source-verification layer for AI reading workflows.

Reader is built around a simple pipeline:

1. Discover an article.
2. Try multiple body providers.
3. Verify that a candidate is the same complete source text.
4. Only then pass it to the AI reading workflow.

The project also includes a blind-first reading UI: answer before reading, read the source, reflect again, then compare with AI assistance.

## Why

AI readers are only as reliable as the text they receive. Search results, reposts, summaries, paywall stubs, and partial mirrors can all look like the original article. Reader separates retrieval from verification so downstream AI does not silently reason over the wrong text.

## Quick start

```bash
python -m pip install -r requirements.txt
python reading_app.py --demo
```

Then open `http://127.0.0.1:8765`.

## Safe defaults

The public build is local-first. Personalization, background refresh, WeRead discovery, WeChat resolution, Zhihu refresh, and source-specific review are opt-in rather than automatic.

Runtime state defaults to `~/.reader`. Do not commit cookies, authorization state, article caches, private reading lists, or downloaded article bodies.

Some source adapters target services that can change independently. Treat them as optional adapters, not as the architectural center of the project.

## Tests

```bash
python -m unittest discover -s tests -v
```

Current release-candidate baseline: 37 tests.

## License

MIT.
