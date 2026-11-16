# Changelog

## 0.1.0

- Initial local web chat interface for DataIndex (`dataindex-ui` command).
- Saved conversations in a sidebar: create, open, rename and delete; each
  question, cited answer and error is kept in `ui_history.sqlite3` in the
  workspace directory.
- Answers arrive over server-sent events with progress ticks; citations show
  document, position, operation, source ID, version, native locator and excerpt.
- Document sidebar: upload Markdown, text or HTML, limit a question to selected
  documents, and remove documents.
- **Connect** dialog that indexes a database server (`POST /api/databases`) from
  a password-free URL, an optional password environment variable name, schema
  and name, and the approved tables and queries. Validation errors never echo
  the submitted values.
- Loopback-only server with a per-run sign-in token, `Host` and `Origin` checks
  and a strict content security policy.
