# Cider Omarchy widget

## Product rules

- Keep Cider traffic on loopback. Do not send `CIDER_API_KEY` to remote hosts.
- Read the token inside `cider-rpc.py`, first from its process environment and then from the user service manager environment. Never add it to command arguments, logs, settings, fixtures, screenshots, or files.
- Use Cider's documented `/api/v1/playback` endpoints.
- Keep one shared service per Omarchy shell so multiple monitors do not duplicate polling.
- Poll lightweight playback state on the configured interval. Fetch the queue only while a panel is open.
- The Cider queue includes history and the current item. Show entries after the last current-track match as Up Next.
- Preserve the built-in Omarchy media widget's visual language and theme tokens.

## Validation

Run `./tests/run`, `omarchy plugin validate .`, and `git diff --check` before delivery.
