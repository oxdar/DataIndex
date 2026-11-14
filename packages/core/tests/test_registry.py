from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from dataindex.models import ExtensionManifest, HealthResult, HealthStatus
from dataindex.registry import ExtensionRegistry


def make_manifest(
    *,
    api_range: str = ">=0.1,<0.2",
    plugin_id: str = "documents",
    distribution: str = "dataindex-ext-documents",
    version: str = "0.1.0",
) -> ExtensionManifest:
    return ExtensionManifest(
        plugin_id=plugin_id,
        distribution=distribution,
        plugin_version=version,
        core_api_range=api_range,
        edition="free",
        license_mode="none",
        capabilities=("index",),
        source_types=("text/markdown",),
        publisher="test-publisher",
    )


class FakeDistribution:
    name = "dataindex-ext-documents"
    version = "0.1.0"

    def __init__(self, manifest: ExtensionManifest) -> None:
        self.name = manifest.distribution
        self.version = manifest.plugin_version
        top_level_module = "dataindex_ext_" + manifest.plugin_id.replace("-", "_")
        self.files = {
            f"{top_level_module}/dataindex-extension.json": json.dumps(asdict(manifest))
        }

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)


class LocatedManifest:
    def __init__(self, content: str) -> None:
        self.content = content

    def read_text(self, encoding: str) -> str:
        assert encoding == "utf-8"
        return self.content


class LocatedManifestDistribution(FakeDistribution):
    def read_text(self, path: str) -> str | None:
        return None

    def locate_file(self, path: str) -> LocatedManifest:
        return LocatedManifest(self.files[path])


class FakeExtension:
    def __init__(self, manifest: ExtensionManifest) -> None:
        self._manifest = manifest

    def manifest(self) -> ExtensionManifest:
        return self._manifest

    def health(self) -> HealthResult:
        return HealthResult(HealthStatus.HEALTHY)


class FakeEntryPoint:
    name = "documents"
    value = "dataindex_ext_documents.plugin:load_plugin"

    def __init__(
        self,
        manifest: ExtensionManifest | None = None,
        *,
        load_error: Exception | None = None,
    ) -> None:
        self.manifest = manifest or make_manifest()
        self.dist = FakeDistribution(self.manifest)
        self.load_error = load_error
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        if self.load_error:
            raise self.load_error
        return lambda: FakeExtension(self.manifest)


def trusted_enabled_registry() -> ExtensionRegistry:
    return ExtensionRegistry(
        trusted_distributions={("dataindex-ext-documents", "0.1.0")},
        enabled_plugin_ids={"documents"},
    )


def test_empty_registry_discovers_no_plugins() -> None:
    assert ExtensionRegistry().discover(()) == ()


def test_discovery_does_not_import_plugin_until_explicitly_loaded() -> None:
    entry_point = FakeEntryPoint()
    registry = trusted_enabled_registry()

    discovered = registry.discover((entry_point,))

    assert discovered[0].compatible
    assert discovered[0].trusted
    assert discovered[0].enabled
    assert discovered[0].healthy is None
    assert entry_point.load_calls == 0

    loaded = registry.load_enabled()

    assert loaded[0].healthy
    assert entry_point.load_calls == 1
    assert "documents" in registry.loaded_extensions


def test_discovery_falls_back_to_distribution_package_files() -> None:
    entry_point = FakeEntryPoint()
    entry_point.dist = LocatedManifestDistribution(entry_point.manifest)

    status = trusted_enabled_registry().discover((entry_point,))[0]

    assert status.compatible
    assert status.trusted
    assert entry_point.load_calls == 0


def test_invalid_entry_point_module_is_rejected_before_path_lookup() -> None:
    entry_point = FakeEntryPoint()
    entry_point.value = "../outside:load_plugin"
    registry = ExtensionRegistry()

    status = registry.discover((entry_point,))[0]

    assert status.error == "entry point target has an invalid module"
    assert not status.compatible
    assert entry_point.load_calls == 0


def test_duplicate_ids_are_reported_and_not_loaded() -> None:
    first = FakeEntryPoint()
    second = FakeEntryPoint()
    registry = trusted_enabled_registry()

    statuses = registry.discover((first, second))

    assert len(statuses) == 1
    assert statuses[0].error == "duplicate plugin entry-point name"
    assert not statuses[0].compatible
    registry.load_enabled()
    assert first.load_calls == second.load_calls == 0


def test_incompatible_plugin_is_not_loaded() -> None:
    manifest = make_manifest(api_range=">=0.2,<0.3")
    entry_point = FakeEntryPoint(manifest)
    registry = trusted_enabled_registry()

    status = registry.discover((entry_point,))[0]

    assert not status.compatible
    assert "outside" in status.error
    registry.load_enabled()
    assert entry_point.load_calls == 0


def test_untrusted_plugin_is_not_loaded() -> None:
    entry_point = FakeEntryPoint()
    registry = ExtensionRegistry(enabled_plugin_ids={"documents"})

    status = registry.discover((entry_point,))[0]

    assert status.compatible
    assert not status.trusted
    registry.load_enabled()
    assert entry_point.load_calls == 0


def test_plugin_load_failure_is_reported_without_hiding_other_plugins(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = FakeEntryPoint(load_error=RuntimeError("broken import"))
    working = FakeEntryPoint()
    working.name = "other"
    working.value = "dataindex_ext_other.plugin:load_plugin"
    other_manifest = make_manifest(
        plugin_id="other",
        distribution="dataindex-ext-other",
    )
    working.manifest = other_manifest
    working.dist = FakeDistribution(other_manifest)
    trusted = {
        ("dataindex-ext-documents", "0.1.0"),
        ("dataindex-ext-other", "0.1.0"),
    }
    registry = ExtensionRegistry(
        trusted_distributions=trusted,
        enabled_plugin_ids={"documents", "other"},
    )

    registry.discover((failing, working))
    statuses = registry.load_enabled()

    assert statuses[0].error == "RuntimeError: broken import"
    assert statuses[0].healthy is False
    assert statuses[1].healthy is True
    assert "Failed to load trusted plugin documents" in caplog.text
