# Privacy & Data Handling

## Why this exists

This system handles reports about real emergencies. That means it has to be
trustworthy with people's information from day one, not as an afterthought
— aligned with Kenya's Data Protection Act, 2019.

## What gets collected

- **Incident reports**: type, area (not exact home address), approximate
  coordinates, a short description, and a timestamp.
- **Officer accounts**: a username and a hashed password. Passwords are
  never stored in plain text (bcrypt hashing, called directly).

No names, phone numbers, or national ID numbers are collected as part of an
**incident report** itself. This is deliberate, not incidental: a reporter
choosing "officers only" visibility on a report (e.g. witnessing a crime in
progress) is protected in part *because* there is no identity field to leak
in the first place — officers see the report content, never who filed it.
The same is true of confirming or disputing someone else's report
(`/report/{id}/vote`): no identity is attached to a vote either.

- **Alert subscribers**: a phone number and an approximate location, collected
  only when someone explicitly opts in via `/subscribers` to receive area
  alerts. This is a separate, deliberate, consent-based signup — not
  something bundled into filing an incident report. It exists for exactly
  one purpose: geo-targeted broadcast alerts (see `architecture.md`,
  Surjection). Phone numbers are not used for anything else and are not
  linked to any incident report someone may separately file.

## Storage

- Incident and officer data lives in a local SQLite database
  (`eis.db`, excluded from version control via `.gitignore`).
- Auth uses signed JWT tokens, not stored sessions; the signing secret is
  read from the `EIS_SECRET_KEY` environment variable. If it isn't set, a
  random secret is generated per process start rather than falling back to
  a fixed value in source - a hardcoded fallback would be visible to
  anyone reading this public repository.

## Training data

The severity-prediction model is trained on **fully synthetic** data
(`scripts/generate_synthetic_data.py`) — generated area names, timestamps,
and incident types. No real incident records are used for training in this
version of the system.

## What's not built yet, but is planned before real deployment

- Formal data retention limits and deletion workflow.
- Role-based access tiers beyond a single "officer" role.
- An audit log of who accessed what, and when.

This document will be updated alongside the code — if a feature here isn't
built yet, it's listed under "not built yet," not implied as already live.
