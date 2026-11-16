# Contributing

DataIndex v0.1.0 is a pre-release. Before starting a larger change, open an
issue to discuss it, and keep each pull request focused on one change.

## Development checks

From the repository root, install the core test extra and run the core suite:

```powershell
uv run --project packages\core --extra test pytest packages\core\tests -q -p no:cacheprovider
```

Build each package independently with `python -m build --wheel --sdist
--outdir <output-directory> <package-directory>`. Extension changes must be
tested from built wheels in clean environments, including core-only and mixed
install/removal checks. The workflows in `.github/workflows` run the complete
checks.

Keep extension source, dependency and licensing boundaries intact. Never add
arbitrary SQL, vector/embedding dependencies, proprietary implementation code,
credentials, or an implied license grant. The root/core/free-extension license
decision is still pending; local builds are validation artifacts only and must
not be redistributed.

## Pull requests

Include source, focused tests, and directly related documentation. Do not
commit virtual environments, build outputs, caches, keys, or local DataIndex
workspaces; see `.gitignore`. Keep reproducible fixtures, dependency lockfiles,
CI configuration, and license/provenance notices in version control. Use
synthetic data in examples and bug reports.
