# VS Code + GitHub Copilot workflow for the FIRST DataIndex build

1. Create a NEW GitHub repository named `DataIndex` (blank or with only this kit) and open the local clone in VS Code.
2. Copy this kit into repository root; the `.github` directory is intentionally included, so enable viewing hidden folders.
3. Ensure GitHub Copilot is installed, sign in, open Chat and select an Agent capable of editing workspace files.
4. Open `.github/prompts/00-architecture.prompt.md` using the VS Code prompt-file picker or refer to it in chat; run **Phase 00 only**.
5. Review architecture output and any proposed license decisions. Merge to main only after the Phase 00 gate.
6. Use a fresh branch for each next phase and invoke `.github/prompts/01-core.prompt.md` etc.
7. At each gate, inspect actual commits and test logs, not just Copilot narration.
8. After Phase 07, create the **first** `v0.1.0` tag only when all wheel-install, authorization and mixed-source tests pass.

## Example local commands after Copilot implements packages
```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -U pip build pytest
python -m pip install -e ./packages/core
python -m pip install -e ./packages/extensions/documents
python -m pip install -e ./packages/extensions/databases
pytest -q
```

These commands are **future** instructions; they will fail in the docs-only initial repository until Copilot creates the packages.

## References
- Official VS Code prompt files: https://code.visualstudio.com/docs/agent-customization/prompt-files
- Instructions: https://code.visualstudio.com/docs/agents/reference/custom-instructions-reference
- Python plugin entry points: https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/
- PageIndex code and MIT license: https://github.com/VectifyAI/PageIndex
