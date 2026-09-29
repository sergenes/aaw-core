# Contributing to Agents At Work Core

Thanks for helping. This is the open-source engine behind Agents At Work; the apps are closed, but everything that runs on your computer lives here.

## Developer Certificate of Origin (DCO)

Every commit must be signed off:

```bash
git commit -s -m "your message"
```

That adds a `Signed-off-by: Your Name <you@example.com>` trailer, certifying you have the right to contribute the change under the MIT license (the [DCO 1.1](https://developercertificate.org/)). A DCO check runs on every pull request; unsigned commits fail it. There is no CLA.

## Development setup

```bash
git clone https://github.com/sergenes/aaw-core
cd aaw-core
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,relay]"
pytest
ruff check .
```

Python 3.11+. The host runs on Linux and macOS; the relay runs anywhere Python does.

## What gets extra scrutiny

Changes to these paths are reviewed more slowly and carefully, because they affect security or compatibility with the closed phone apps:

- `aaw_core/encryption.py`: the AES-256-GCM envelope must stay byte-compatible with the apps.
- Pairing and the QR payload.
- `aaw_core/transport/` and `relay/`: event and command shapes are a contract with the apps (see `docs/protocol.contract.md`).

## Before opening a pull request

- For anything non-trivial, open an issue or discussion first so you do not spend a weekend on something that conflicts with the roadmap.
- CI must be green: build, tests, lint, and the secret scanner.
- Never commit secrets, keys, tokens, or anything pointing at a specific hosted project. Configuration comes from environment variables and `~/.aaw/config.json`; the code has no hosted defaults.

## How changes land

`main` is protected: nobody pushes to it directly, the maintainer included.
Every change, however small, goes through a pull request:

1. Branch from an up-to-date `main` (`git switch -c fix/short-name`).
2. Commit with `git commit -s` (see the DCO above).
3. Push the branch and open a pull request against `main`.
4. CI must pass, and every review conversation must be resolved.
5. The maintainer merges it, and the branch is deleted after the merge.

Never force-push `main`, and never rewrite a commit that is already on it: release builds record the aaw-core commit they ship.

## Releasing (maintainers)

A release publishes to PyPI by hand; no workflow uploads anything, and CI only runs the tests.
The version lives in `aaw_core/__init__.py` (`__version__`), which the build reads.

1. Open a pull request that bumps `__version__` (0.x: a minor for features, a patch for fixes) and merge it once CI is green.
2. Update a local `main` to the merged commit, and check `git status` is clean.
3. Tag that commit with an annotated tag and push the tag (tags are not blocked by the branch protection):
   `git tag -a v0.1.1 -m "aaw-core 0.1.1"` and `git push origin v0.1.1`.
4. Build and upload from a clean tree: `rm -rf dist && python -m build && twine check dist/* && twine upload dist/*`.
5. Create the GitHub release from the tag with the notes: `gh release create v0.1.1 --title "aaw-core 0.1.1" --notes-file <notes>`.
6. Check `pip install aaw-core==0.1.1` in a fresh virtualenv.

The Agents At Work Mac app and the Linux installer ship the engine only after this: their release tooling refuses a version that is not on PyPI or a checkout with uncommitted changes.

## How this project is developed

The engine is developed here, in the open. It was assembled by porting the host from the maintainer's product, Agents At Work, and it is now the engine that product runs: the Mac app bundles it, and the hosted relay is this relay. Good contributions here reach both.
