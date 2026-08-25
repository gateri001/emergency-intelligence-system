# Privacy & Data Handling

## Why this exists

This system handles reports about real emergencies. That means it has to be
trustworthy with people's information from day one, not as an afterthought
— aligned with Kenya's Data Protection Act, 2019.

## What gets collected

- **Incident reports**: type, area (not exact home address), approximate
  coordinates, a short description, and a timestamp.
- **Officer accounts**: a username and a hashed password. Passwords are
  never stored in plain text (bcrypt hashing via `passlib`).

No names, phone numbers, or national ID numbers are collected as part of an
incident report today. If future versions add contact fields (e.g. for
follow-up on a citizen report), those fields will be clearly optional,
separately protected, and covered by an update to this document before they
ship — not bundled in silently.

## Storage

- Incident and officer data lives in a local SQLite database
  (`eis.db`, excluded from version control via `.gitignore`).
- Auth uses signed JWT tokens, not stored sessions; the signing secret is
  read from an environment variable, never hardcoded in source.

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
