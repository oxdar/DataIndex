"""Safe metadata discovery and explicit loading of trusted extensions."""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from importlib.metadata import EntryPoint, entry_points

from packaging.utils import canonicalize_name
from packaging.version import Version

from dataindex.exceptions import PluginLoadError, PluginManifestError
from dataindex.models import DataIndexExtension, ExtensionManifest, HealthStatus


LOGGER = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "dataindex.extensions"


@dataclass(frozen=True, slots=True)
class PluginStatus:
    plugin_id: str
    distribution: str | None
    distribution_version: str | None
    installed: bool
    discovered: bool
    compatible: bool
    trusted: bool
    enabled: bool
    entitled: bool | None = None
    tenant_authorized: bool | None = None
    healthy: bool | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    entry_point: EntryPoint
    manifest: ExtensionManifest


class ExtensionRegistry:
    """Discover entry points without importing plugin code.

    The allowlist matches distribution name and version; artifact signature or
    hash verification remains the installer's responsibility.
    """

    def __init__(
        self,
        *,
        core_api_version: str = "0.1",
        trusted_distributions: Iterable[tuple[str, str]] = (),
        enabled_plugin_ids: Iterable[str] = (),
    ) -> None:
        self._core_api_version = core_api_version
        self._trusted_distributions = {
            (canonicalize_name(name), str(Version(version)))
            for name, version in trusted_distributions
        }
        self._enabled_plugin_ids = set(enabled_plugin_ids)
        self._candidates: dict[str, _Candidate] = {}
        self._statuses: dict[str, PluginStatus] = {}
        self._loaded: dict[str, DataIndexExtension] = {}

    @property
    def statuses(self) -> tuple[PluginStatus, ...]:
        return tuple(self._statuses[key] for key in sorted(self._statuses))

    @property
    def loaded_extensions(self) -> Mapping[str, DataIndexExtension]:
        return dict(self._loaded)

    def discover(
        self, discovered_entry_points: Iterable[EntryPoint] | None = None
    ) -> tuple[PluginStatus, ...]:
        """Read manifests and validate compatibility/trust without importing plugins."""
        self._candidates.clear()
        self._statuses.clear()
        self._loaded.clear()
        points = tuple(
            entry_points(group=ENTRY_POINT_GROUP)
            if discovered_entry_points is None
            else discovered_entry_points
        )
        name_counts = Counter(point.name for point in points)

        for point in points:
            distribution = getattr(point.dist, "name", None)
            version = getattr(point.dist, "version", None)
            initial = PluginStatus(
                plugin_id=point.name,
                distribution=distribution,
                distribution_version=version,
                installed=True,
                discovered=True,
                compatible=False,
                trusted=False,
                enabled=False,
            )
            if name_counts[point.name] > 1:
                self._statuses[point.name] = replace(
                    initial, error="duplicate plugin entry-point name"
                )
                continue
            try:
                manifest = self._read_manifest(point)
                if manifest.plugin_id != point.name:
                    raise PluginManifestError(
                        "manifest plugin_id must match the entry-point name"
                    )
                if not distribution or not version:
                    raise PluginManifestError(
                        "entry point must identify its installed distribution and version"
                    )
                if canonicalize_name(manifest.distribution) != canonicalize_name(
                    distribution
                ):
                    raise PluginManifestError(
                        "manifest distribution does not match installed distribution"
                    )
                if Version(manifest.plugin_version) != Version(version):
                    raise PluginManifestError(
                        "manifest version does not match installed distribution version"
                    )
                compatible = manifest.supports_core_api(self._core_api_version)
                trusted = (
                    canonicalize_name(distribution),
                    str(Version(version)),
                ) in self._trusted_distributions
                enabled = manifest.plugin_id in self._enabled_plugin_ids
                self._statuses[point.name] = PluginStatus(
                    plugin_id=point.name,
                    distribution=distribution,
                    distribution_version=version,
                    installed=True,
                    discovered=True,
                    compatible=compatible,
                    trusted=trusted,
                    enabled=enabled,
                    error=None
                    if compatible
                    else f"core API {self._core_api_version} is outside "
                    f"{manifest.core_api_range}",
                )
                if compatible:
                    self._candidates[point.name] = _Candidate(point, manifest)
            except (
                AttributeError,
                OSError,
                UnicodeError,
                ValueError,
                TypeError,
                PluginManifestError,
            ) as error:
                LOGGER.warning("Could not discover plugin %s: %s", point.name, error)
                self._statuses[point.name] = replace(initial, error=str(error))

        return self.statuses

    def load_enabled(self) -> tuple[PluginStatus, ...]:
        """Load only plugins that are compatible, trusted and explicitly enabled."""
        for plugin_id, candidate in self._candidates.items():
            status = self._statuses[plugin_id]
            if not (status.compatible and status.trusted and status.enabled):
                continue
            if plugin_id in self._loaded:
                continue
            try:
                factory = candidate.entry_point.load()
                if not callable(factory):
                    raise PluginLoadError("entry point target is not callable")
                extension = factory()
                if extension.manifest() != candidate.manifest:
                    raise PluginManifestError(
                        "runtime manifest differs from static manifest"
                    )
                health = extension.health()
                if health.status is not HealthStatus.HEALTHY:
                    self._statuses[plugin_id] = replace(
                        status,
                        healthy=False,
                        error=health.diagnostic or f"plugin health is {health.status.value}",
                    )
                    continue
                self._loaded[plugin_id] = extension
                self._statuses[plugin_id] = replace(
                    status, healthy=True, error=None
                )
            except Exception as error:
                LOGGER.exception("Failed to load trusted plugin %s", plugin_id)
                self._statuses[plugin_id] = replace(
                    status,
                    healthy=False,
                    error=f"{type(error).__name__}: {error}",
                )
        return self.statuses

    @staticmethod
    def _read_manifest(point: EntryPoint) -> ExtensionManifest:
        module_name = point.value.partition(":")[0].strip()
        if not module_name or any(
            not part.isidentifier() for part in module_name.split(".")
        ):
            raise PluginManifestError("entry point target has an invalid module")
        top_level_module = module_name.split(".", maxsplit=1)[0]
        manifest_path = f"{top_level_module}/dataindex-extension.json"
        content = point.dist.read_text(manifest_path)
        if content is None:
            try:
                content = point.dist.locate_file(manifest_path).read_text(
                    encoding="utf-8"
                )
            except (AttributeError, OSError, UnicodeError) as error:
                raise PluginManifestError(
                    f"manifest file not found: {manifest_path}"
                ) from error
        data = json.loads(content)
        if not isinstance(data, dict):
            raise PluginManifestError("manifest root must be a JSON object")
        try:
            return ExtensionManifest.from_mapping(data)
        except ValueError as error:
            raise PluginManifestError(str(error)) from error
