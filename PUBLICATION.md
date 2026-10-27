# Public distribution and evidence handling

This repository publishes the demonstration clients, local test harnesses, documentation and synthetic measurements under the SAWM-io organisation.

## What is not published

- No active API key, access token, password, private SSH key or populated `.env` file.
- No customer conversations, business records or production database dump.
- No implementation source of the private SAWM, Basira, TSST or ODVL engines. `vendor/basira_client.py` is an HTTP client; `reference_replay.py` requires a separately authorised private source tree.

The `.env.example` file contains placeholders only. Users must create their own Cloud credentials, store them outside the repository with restricted file permissions, and obtain the necessary scopes and entity grants. Sample Cloud identifiers cannot access the original laboratory environment.

## Published evidence is pseudonymised

Real Cloud space/entity/job/session/evidence identifiers were consistently replaced with demonstration identifiers across the Git history. Internal laboratory network addresses and virtual MAC identifiers, where present, were replaced with documentation addresses. The private monorepo name was removed. Unredacted originals remain in a separate private archive outside this repository.

Numerical results, timings, counters, synthetic message payloads and reported outcomes were retained. These are publication copies, not byte-for-byte copies of the original Cloud receipts. Their checksums describe the published files; do not use the substituted identifiers to query the original Cloud records. Recompute results using your own authorised environment.

The reviewed history contains six source commits per repository, plus the publication commit. Automated Gitleaks 8.30.1 scanning and manual review of text, PDFs and (where present) synthetic SQLite evidence found no active credentials. These checks describe the reviewed snapshot; do not commit credentials in future contributions.

## Scope

The local demonstrations exercise the functionality stated in their validation reports. They do not grant access to private engines or include a production security guarantee. Public visibility is not a certification and does not change Cloud account permissions.
