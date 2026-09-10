# Security policy

`context-proof` is designed to run on captured agent context, including in
offline CI. The runtime package uses only the Python standard library. It does
not make network requests, call a model, send telemetry, import provider SDKs,
execute input strings, or read files other than the path supplied to the CLI.

The checker does not print message content in findings. It can print caller-
chosen ids, paths, hashes, counts, and remediation text, so treat reports and
fixtures as part of the sensitivity boundary of the surrounding project.
Prefer synthetic or redacted fixtures for public repositories.

If you discover a security issue, please open a private GitHub security report
for [thisbejim/context-proof](https://github.com/thisbejim/context-proof/security)
when available. For ordinary bugs, use the public issue tracker.
