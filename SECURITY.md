# Security policy

This project handles end-to-end encryption and remote control of coding agents. Please report vulnerabilities privately, not in the public issue tracker.

## Reporting

- Preferred: open a private report through **GitHub Security Advisories** on this repository ("Report a vulnerability").
- Or email **hello@agentsatwork.app** with the details.

Include what you found, how to reproduce it, and the impact you believe it has. You will get an acknowledgement, and a fix or a mitigation plan, before any public disclosure.

## Scope

- The host (`aaw_core/`): the daemon, hooks, pairing, and the `aaw` command.
- The relay (`relay/`).
- The encryption envelope and key handling.

The closed phone and macOS apps are out of scope here; report those to the same email.
