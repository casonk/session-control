# Security Policy

## Reporting

Do not file sensitive disclosures in public issues.

Report security issues privately to the repository owner or maintainer instead
of publishing exploit details in a public issue or pull request.

## Scope

This repository must not become a place to store live secrets, credentials,
tokens, private keys, personal data, raw assistant transcripts, or private
environment details.

- Treat `CHATHISTORY.md`, `REFS-LOCAL.md`, `config/*.local`, session previews,
  and trash contents as local-only operational data.
- Do not commit machine-specific absolute filesystem paths, hostnames, internal
  endpoint addresses, account identifiers, or copied session transcripts.
- Keep tracked examples and tests synthetic. Real assistant transcripts can
  contain credentials, private data, file paths, account identifiers, and
  pasted terminal output.
- The standalone Flask app must default to loopback. Wider exposure requires an
  explicit trust boundary such as Caddy with mTLS, WireGuard, or equivalent
  private access control.
- State-changing routes must retain CSRF protection even when the app is
  deployed behind a trusted proxy.

## Delete Safety

Session deletion moves files into a local trash directory by default. This
prevents accidental permanent loss while still removing old sessions from the
provider's normal session picker.

Do not add hard-delete behavior unless it is opt-in, tested, and clearly
separated from the default path.
