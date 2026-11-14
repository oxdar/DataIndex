# Synthetic premium extension fixture

This independently packaged wheel exists only to test DataIndex's commercial
extension contract. It returns a fixed synthetic value and does not access
customer data, implement a paid product, or communicate with a licensing
service.

The test contract builds and installs this wheel beside the core and free
DocumentIndex wheel, then checks missing entitlement, grant, expiry, revocation,
tenant isolation, provenance, and continued free-tool execution:

```powershell
python -m build --wheel --outdir dist\core packages\core
python -m build --wheel --outdir dist\documents packages\extensions\documents
python -m build --wheel --outdir dist\databases packages\extensions\databases
python -m build --wheel --outdir dist\mock-premium tests\plugins\mock_premium
python -m venv .venv-commercial
.venv-commercial\Scripts\python -m pip install pytest dist\core\*.whl dist\documents\*.whl dist\databases\*.whl dist\mock-premium\*.whl
.venv-commercial\Scripts\python -m pip check
.venv-commercial\Scripts\python -m pytest tests\plugins\mock_premium\tests -q -p no:cacheprovider
```

The in-memory entitlement provider lives in the test suite. It is not a billing
provider or a production security boundary. Do not use this package to protect
distributed proprietary code. Python plugins run in-process and are not
sandboxed.
