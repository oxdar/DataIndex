# Changelog

## 0.1.0

- Initial independently installable SQLite schema and bounded-query extension.
- Added `register_database_config` (register from the JSON form of the approved
  columns and queries), `database_source_version` and `unregister_database`,
  which the local-mode SDK uses to persist and restore `submit_database`.
- The read authorizer allows SQLite's empty-column table read, which an approved
  `COUNT(*)` query produces, on approved tables only. Column reads are still
  checked against the approved columns.
