# Contributing

DataIndex v0.1.0 is a greenfield, phase-gated implementation. Read
`AGENTS.md`, `.github/copilot-instructions.md`, the scope, acceptance criteria,
and the active phase plan before making changes. Implement one phase at a time
and stop at its review gate.

## Development checks

From the repository root, install the core test extra and run the core suite:

```powershell
uv run --project packages\core --extra test pytest packages\core\tests -q -p no:cacheprovider
```

Build each package independently with `python -m build --wheel --sdist
--outdir <output-directory> <package-directory>`. Extension changes must be
tested from built wheels in clean environments, including core-only and mixed
install/removal checks. See `docs/REPOSITORY_AND_PACKAGING.md` for the required
matrix and `docs/PHASE_STATUS.md` for actual evidence.

Keep extension source, dependency and licensing boundaries intact. Never add
arbitrary SQL, vector/embedding dependencies, proprietary implementation code,
credentials, or an implied license grant. The root/core/free-extension license
decision is still pending; local builds are validation artifacts only and must
not be redistributed.
