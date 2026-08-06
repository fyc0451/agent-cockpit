# Security Policy

## Supported versions

Security fixes are applied to the latest commit on `main`. Agent Cockpit's policy
covers this repository only; vulnerabilities in Herdr, Agent Mail, or an agent CLI
should be reported to the corresponding project.

## Reporting a vulnerability

Please use GitHub's private security advisory flow for this repository. Do not open
a public issue containing exploit details. If private advisories are unavailable,
open an issue asking the maintainer for a private contact channel without including
the vulnerability details.

We aim to acknowledge a report within seven days. Include affected versions,
reproduction steps, impact, and any suggested mitigation when possible.

## Deployment boundary

Agent Cockpit exposes terminal, file, task, and agent-control capabilities as the
operating-system user that runs it. It is intended for one trusted operator, not as
a multi-user authorization system.

- Keep the default `127.0.0.1` bind unless remote access is required.
- A non-loopback bind requires `COCKPIT_TOKEN`, including private/LAN addresses.
- Use HTTPS or Tailscale Serve for remote access. Plain HTTP exposes the login
  session cookie to anyone able to observe local network traffic.
- Do not expose the service directly to the public Internet.
- Rotate `COCKPIT_TOKEN` by changing `.env` and restarting the service.
