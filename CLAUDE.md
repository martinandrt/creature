# Rules for agents building this repo

- **Never write to `registry/` by hand.** Only a creature run may add or change a capability there.
  Hand-written or seeded "generated" code is the one unforgivable fake.
- Everything in English: code, comments, docs, commit messages. Comments short and only where the code is not obvious.
- One feature at a time: setup, tests, quality pass, edge cases, refactor, docs, commit. Then report.
- No secrets in the repo. Keys are read at runtime from the file named by `CREATURE_SECRETS`.
- Generated code runs only inside the Docker sandbox, never on the host.
- The creature's model calls get no outside context: isolated `claude -p`, no tools, no MCP, no user settings.
- Keep it plain Python 3.12, standard library first.
