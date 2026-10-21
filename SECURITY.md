# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a vulnerability

Please report security issues privately via GitHub Security Advisories
("Report a vulnerability" on this repository's Security tab). Do not open a
public issue for a vulnerability.

## Security model

Takeout Audit processes **untrusted archive files** (a Takeout set can come
from anywhere) on the user's own machine:

- `audit` never writes to disk and never extracts archive members.
- `extract` writes only inside the user-named target directory, strips the
  `Takeout/` prefix, refuses a non-empty target, and logs every skipped
  duplicate. Path traversal entries inside archives (`../`) are a known
  class of concern: report any case where an entry escapes the target
  directory as a security issue.
- There is no network code in the project. Any future network behaviour is
  a security regression by definition — see CONTRIBUTING.md.

Takeout sets contain deeply personal data. The tool's design goal is that
using it never makes that data less safe: no uploads, no analytics, no
copies outside the target you chose.
