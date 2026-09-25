## What this changes

<!-- One or two sentences. Link the issue or discussion if there is one. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`), per the DCO in CONTRIBUTING.md.
- [ ] No secrets, tokens, or hosted-project defaults were added; config comes from env or `~/.aaw/config.json`.
- [ ] Tests pass locally (`pytest`) and lint is clean (`ruff check .`).
- [ ] If this touches encryption, pairing, `aaw_core/transport/`, or `relay/`: the event/command shapes and the encryption envelope stay compatible with the phone apps (see `docs/protocol.contract.md`).
