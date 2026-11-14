# Mandatory release acceptance matrix

## Global requirements
- All release builds install from wheels in clean isolated Python environments; no reliance on editable path imports for release pass.
- `pip check` passes after each supported install combination.
- `pytest -q`, type checks, linter and package-build checks pass; exact commands/results included in release report.
- Zero embeddings, zero vector DB, zero runtime `pageindex` import; dependency tree reviewed.
- Free core does not contact any license server or LLM provider on import/start.
- Source data stays authoritative; citations include verified source/version/locator.
- No premium source code or private secrets in public repo.

## Phase gates
| Phase | Gate | Positive evidence | Negative test |
|---|---|---|---|
| 00 | Greenfield ADRs and specs | Full architecture, extension ABI, license policy | Explicitly no fiction about existing implementation |
| 01 | Core wheel | Install/import/list plugins without extra deps | Core-only environment does not import extension libs |
| 02 | Persistent index | Reopen indexed hierarchy, graph and source locators | Reject duplicate/corrupt/version-conflicting nodes |
| 03 | Documents free wheel | 3 format fixture retrieval with exact section locator | Unsupported format fails clearly; uninstall isolation |
| 04 | Databases free wheel | SQLite schema + allowed filtered aggregation correct | DML, unbounded reads and unauthorized table denied |
| 05 | Reasoning | Multi-source answer with real locator, bounded tool calls | Hallucinated locator, prompt injection, no-source abstention |
| 06 | Commercial readiness | Separate synthetic premium wheel deny→grant→revoke | No license never executes tool; free plugins unaffected |
| 07 | v0.1.0 release | CLI + SDK, CI matrix, CHANGELOG, sdist/wheel; examples work | Fresh install lacks no hidden dependency; secrets scan |

## Representative golden question
"For the overdue invoices in the SQLite demo DB, identify the relevant contract payment terms in the Markdown file and return the invoice total with evidence."
Correctness must be verified deterministically against fixtures. This is NOT proof of general market accuracy.

## Additional standards
Maintain `docs/PHASE_STATUS.md` with date, commit, actual test commands, observed pass/fail and remaining issues. Never mark tests passed without running them. See each detailed phase plan for file-level deliverables.
