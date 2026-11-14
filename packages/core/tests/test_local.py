from __future__ import annotations

import importlib.metadata
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataindex import local
from dataindex.exceptions import ConfigurationError
from dataindex.local import (
    MAX_SUBMITTED_FILE_BYTES,
    DatabaseRegistrar,
    DocumentRegistrar,
    FileKind,
    LocalAuthorizer,
    classify_file,
    content_version,
    first_party_plugins,
    free_capabilities,
    install_hint,
    load_local_registry,
    read_submitted_file,
    resolve_submitted_path,
    resolve_trusted_plugins,
    source_id_for,
)
from dataindex.models import HealthStatus, SourceRef
from dataindex.registry import ExtensionRegistry, PluginStatus
from dataindex.workspace import WorkspaceDocument
from local_layer_fakes import (
    DATABASES_DISTRIBUTION,
    DOCUMENTS_DISTRIBUTION,
    PREMIUM_DISTRIBUTION,
    BasicExtension,
    FakeDocumentsPlugin,
    FakeEntryPoint,
    documents_entry_point,
    documents_manifest,
    free_manifest,
    premium_manifest,
)

TRUSTED_DOCUMENTS = [(DOCUMENTS_DISTRIBUTION, "0.1.0")]
SOURCE = SourceRef("doc-1", "text/markdown", "sha256:aaa")


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    packages: dict[str, str] = {}

    def version(name: str) -> str:
        if name not in packages:
            raise importlib.metadata.PackageNotFoundError(name)
        return packages[name]

    monkeypatch.setattr(
        local,
        "metadata",
        SimpleNamespace(
            version=version, PackageNotFoundError=importlib.metadata.PackageNotFoundError
        ),
    )
    return packages


def by_id(registry: ExtensionRegistry) -> dict[str, PluginStatus]:
    return {status.plugin_id: status for status in registry.statuses}


# ---------------------------------------------------------------------------
# Which plugins local mode trusts


def test_default_trusts_installed_first_party_plugins_at_their_installed_versions(
    installed: dict[str, str],
) -> None:
    installed.update(
        {
            "dataindex-ext-documents": "0.1.3",
            "dataindex-ext-databases": "0.1.0",
            PREMIUM_DISTRIBUTION: "0.1.0",
            "requests": "2.32.0",
        }
    )
    expected = (("dataindex-ext-documents", "0.1.3"), ("dataindex-ext-databases", "0.1.0"))

    assert first_party_plugins() == expected
    assert resolve_trusted_plugins(None) == expected


def test_default_skips_first_party_plugins_that_are_not_installed(
    installed: dict[str, str],
) -> None:
    assert resolve_trusted_plugins(None) == ()

    installed["dataindex-ext-databases"] = "0.1.0"

    assert resolve_trusted_plugins(None) == (("dataindex-ext-databases", "0.1.0"),)


def test_a_distribution_name_trusts_the_version_installed_right_now(
    installed: dict[str, str],
) -> None:
    installed["acme-plugin"] = "2.0.1"

    assert resolve_trusted_plugins(["acme-plugin"]) == (("acme-plugin", "2.0.1"),)
    assert resolve_trusted_plugins(["  acme-plugin "]) == (("acme-plugin", "2.0.1"),)


def test_a_name_and_version_pair_needs_no_installation(installed: dict[str, str]) -> None:
    assert resolve_trusted_plugins([("acme-plugin", " 1.2.3 ")]) == (("acme-plugin", "1.2.3"),)


def test_a_given_list_replaces_the_default_and_an_empty_list_trusts_nothing(
    installed: dict[str, str],
) -> None:
    installed.update({"dataindex-ext-documents": "0.1.0", "acme": "1.0"})

    assert resolve_trusted_plugins(["acme"]) == (("acme", "1.0"),)
    assert resolve_trusted_plugins([]) == ()
    assert resolve_trusted_plugins(()) == ()


def test_any_iterable_is_accepted_and_duplicates_collapse_in_order(
    installed: dict[str, str],
) -> None:
    installed["acme"] = "1.0"
    items = iter([("b", "2"), "acme", ("b", "2"), ("acme", "1.0")])

    assert resolve_trusted_plugins(items) == (("b", "2"), ("acme", "1.0"))


@pytest.mark.parametrize("value", ["dataindex-ext-documents", b"dataindex-ext-documents", ""])
def test_a_lone_string_is_not_a_list_of_plugins(value: str | bytes) -> None:
    with pytest.raises(ConfigurationError, match="not a single string"):
        resolve_trusted_plugins(value)  # type: ignore[arg-type]


def test_an_uninstalled_distribution_name_is_rejected(installed: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError, match="'ghost'.*not installed"):
        resolve_trusted_plugins(["ghost"])


@pytest.mark.parametrize(
    "item",
    [
        "",
        "   ",
        ("a",),
        ("a", "1", "extra"),
        ["a", "1"],
        ("a", ""),
        ("a", " "),
        ("a", 1),
        (1, "1"),
        None,
        5,
        b"a",
        {"a": "1"},
    ],
)
def test_malformed_trust_entries_are_rejected(
    installed: dict[str, str], item: object
) -> None:
    with pytest.raises(ConfigurationError, match="distribution name or a"):
        resolve_trusted_plugins([item])  # type: ignore[list-item]


@pytest.mark.parametrize("version", ["latest", "1.x", "v-1", "1.0.0.bad"])
def test_an_invalid_version_is_rejected(installed: dict[str, str], version: str) -> None:
    with pytest.raises(ConfigurationError, match="invalid version"):
        resolve_trusted_plugins([("acme", version)])


def test_an_installed_distribution_with_an_invalid_version_is_rejected(
    installed: dict[str, str],
) -> None:
    installed["weird"] = "banana"

    with pytest.raises(ConfigurationError, match="invalid version"):
        resolve_trusted_plugins(["weird"])


# ---------------------------------------------------------------------------
# Loading trusted plugins


def test_trusted_first_party_plugin_is_enabled_and_loaded_without_more_configuration() -> None:
    entry_point = documents_entry_point()

    registry = load_local_registry(TRUSTED_DOCUMENTS, [entry_point])

    status = by_id(registry)["documents"]
    assert (status.compatible, status.trusted, status.enabled, status.healthy) == (
        True,
        True,
        True,
        True,
    )
    assert status.error is None
    assert list(registry.loaded_extensions) == ["documents"]
    assert entry_point.load_calls == 1


def test_default_discovery_reads_the_dataindex_extensions_entry_point_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups: list[str] = []
    entry_point = documents_entry_point()

    def fake_entry_points(*, group: str) -> tuple[FakeEntryPoint, ...]:
        groups.append(group)
        return (entry_point,)

    monkeypatch.setattr(local, "entry_points", fake_entry_points)

    registry = load_local_registry(TRUSTED_DOCUMENTS)

    assert groups == ["dataindex.extensions"]
    assert list(registry.loaded_extensions) == ["documents"]


def test_no_installed_plugins_gives_an_empty_registry() -> None:
    registry = load_local_registry([], [])

    assert registry.statuses == ()
    assert registry.loaded_extensions == {}


def test_plugin_from_another_distribution_is_not_trusted_or_imported() -> None:
    stranger = FakeEntryPoint(documents_manifest(distribution="acme-documents"))

    registry = load_local_registry(TRUSTED_DOCUMENTS, [stranger])

    status = by_id(registry)["documents"]
    assert (status.compatible, status.trusted, status.enabled) == (True, False, False)
    assert status.healthy is None
    assert registry.loaded_extensions == {}
    assert stranger.load_calls == 0


def test_a_plugin_from_another_distribution_loads_once_it_is_allowlisted() -> None:
    stranger = FakeEntryPoint(documents_manifest(distribution="acme-documents"))

    registry = load_local_registry([("acme-documents", "0.1.0")], [stranger])

    assert by_id(registry)["documents"].trusted is True
    assert list(registry.loaded_extensions) == ["documents"]
    assert stranger.load_calls == 1


def test_trust_is_pinned_to_the_exact_installed_version() -> None:
    upgraded = FakeEntryPoint(documents_manifest(version="0.1.1"))

    untrusted = load_local_registry(TRUSTED_DOCUMENTS, [upgraded])

    assert by_id(untrusted)["documents"].trusted is False
    assert untrusted.loaded_extensions == {}
    assert upgraded.load_calls == 0

    trusted = load_local_registry([(DOCUMENTS_DISTRIBUTION, "0.1.1")], [upgraded])

    assert list(trusted.loaded_extensions) == ["documents"]
    assert upgraded.load_calls == 1


def test_distribution_names_match_in_canonical_form() -> None:
    entry_point = documents_entry_point()

    registry = load_local_registry([("DataIndex_Ext.Documents", "0.1.0")], [entry_point])

    assert list(registry.loaded_extensions) == ["documents"]


def test_a_package_impersonating_a_first_party_plugin_is_not_trusted() -> None:
    impostor = FakeEntryPoint(documents_manifest())
    impostor.dist.name = "evil-documents"  # its manifest still claims the first-party name

    registry = load_local_registry(TRUSTED_DOCUMENTS, [impostor])

    status = by_id(registry)["documents"]
    assert (status.compatible, status.trusted, status.enabled) == (False, False, False)
    assert "does not match installed distribution" in (status.error or "")
    assert impostor.load_calls == 0
    assert registry.loaded_extensions == {}


def test_commercial_plugin_is_not_trusted_by_the_default_trust_list(
    installed: dict[str, str],
) -> None:
    installed.update({DOCUMENTS_DISTRIBUTION: "0.1.0", PREMIUM_DISTRIBUTION: "0.1.0"})
    premium = FakeEntryPoint(premium_manifest())
    documents = documents_entry_point()

    registry = load_local_registry(resolve_trusted_plugins(None), [premium, documents])

    statuses = by_id(registry)
    assert statuses["premium-demo"].trusted is False
    assert statuses["premium-demo"].enabled is False
    assert premium.load_calls == 0
    assert list(registry.loaded_extensions) == ["documents"]


def test_trusting_a_commercial_plugin_still_grants_none_of_its_capabilities() -> None:
    premium = FakeEntryPoint(premium_manifest())

    registry = load_local_registry([(PREMIUM_DISTRIBUTION, "0.1.0")], [premium])

    manifest = registry.loaded_extensions["premium-demo"].manifest()
    authorizer = LocalAuthorizer("local")
    source = SourceRef("doc-9", "application/vnd.dataindex.mock-premium", "v1")
    authorizer.grant(source, free_capabilities(manifest))
    assert authorizer.is_authorized("local", source, "source.access")
    assert not authorizer.is_authorized("local", source, "premium.demo.read")


def test_incompatible_plugin_is_reported_and_never_loaded() -> None:
    old = FakeEntryPoint(documents_manifest(core_api_range=">=0.2,<0.3"))
    other = FakeEntryPoint(free_manifest("databases", DATABASES_DISTRIBUTION))

    registry = load_local_registry(
        [(DOCUMENTS_DISTRIBUTION, "0.1.0"), (DATABASES_DISTRIBUTION, "0.1.0")],
        [old, other],
    )

    statuses = by_id(registry)
    assert statuses["documents"].compatible is False
    assert statuses["documents"].enabled is False
    assert "outside" in (statuses["documents"].error or "")
    assert old.load_calls == 0
    assert list(registry.loaded_extensions) == ["databases"]


def test_a_plugin_that_fails_to_load_does_not_stop_the_others() -> None:
    broken = FakeEntryPoint(documents_manifest(), load_error=RuntimeError("boom"))
    healthy = FakeEntryPoint(free_manifest("databases", DATABASES_DISTRIBUTION))

    registry = load_local_registry(
        [(DOCUMENTS_DISTRIBUTION, "0.1.0"), (DATABASES_DISTRIBUTION, "0.1.0")],
        [broken, healthy],
    )

    statuses = by_id(registry)
    assert statuses["documents"].healthy is False
    assert "boom" in (statuses["documents"].error or "")
    assert statuses["databases"].healthy is True
    assert list(registry.loaded_extensions) == ["databases"]


def test_an_unhealthy_plugin_is_reported_and_not_loaded() -> None:
    manifest = documents_manifest()
    sick = FakeEntryPoint(
        manifest, extension=BasicExtension(manifest, health=HealthStatus.UNAVAILABLE)
    )

    registry = load_local_registry(TRUSTED_DOCUMENTS, [sick])

    assert by_id(registry)["documents"].healthy is False
    assert registry.loaded_extensions == {}


def test_a_plugin_without_a_readable_manifest_does_not_stop_the_others() -> None:
    manifestless = FakeEntryPoint(documents_manifest())
    manifestless.dist.files.clear()
    healthy = FakeEntryPoint(free_manifest("databases", DATABASES_DISTRIBUTION))

    registry = load_local_registry(
        [(DOCUMENTS_DISTRIBUTION, "0.1.0"), (DATABASES_DISTRIBUTION, "0.1.0")],
        [manifestless, healthy],
    )

    statuses = by_id(registry)
    assert statuses["documents"].enabled is False
    assert "manifest file not found" in (statuses["documents"].error or "")
    assert manifestless.load_calls == 0
    assert list(registry.loaded_extensions) == ["databases"]


def test_duplicate_plugin_names_are_never_loaded() -> None:
    first = documents_entry_point()
    second = documents_entry_point()

    registry = load_local_registry(TRUSTED_DOCUMENTS, [first, second])

    assert "duplicate" in (by_id(registry)["documents"].error or "")
    assert (first.load_calls, second.load_calls) == (0, 0)
    assert registry.loaded_extensions == {}


# ---------------------------------------------------------------------------
# Source access in local mode


def test_free_capabilities_are_those_of_a_free_plugin() -> None:
    assert free_capabilities(documents_manifest()) == {"document.index", "document.retrieve"}


def test_commercial_plugins_have_no_locally_granted_capabilities() -> None:
    assert free_capabilities(premium_manifest()) == frozenset()


def test_authorizer_allows_only_the_granted_source_for_the_local_tenant() -> None:
    authorizer = LocalAuthorizer("local")
    assert not authorizer.is_authorized("local", SOURCE, "source.access")

    authorizer.grant(SOURCE, {"document.index"})

    assert authorizer.is_authorized("local", SOURCE, "source.access")
    assert authorizer.is_authorized("local", SOURCE, "document.index")
    assert not authorizer.is_authorized("local", SOURCE, "document.retrieve")
    assert not authorizer.is_authorized("someone-else", SOURCE, "source.access")
    assert not authorizer.is_authorized("", SOURCE, "source.access")
    assert not authorizer.is_authorized("LOCAL", SOURCE, "source.access")


@pytest.mark.parametrize(
    "other",
    [
        SourceRef("doc-2", "text/markdown", "sha256:aaa"),
        SourceRef("doc-1", "text/markdown", "sha256:bbb"),
        SourceRef("doc-1", "text/plain", "sha256:aaa"),
    ],
    ids=["other-source", "other-version", "other-type"],
)
def test_authorizer_pins_a_grant_to_the_exact_source(other: SourceRef) -> None:
    authorizer = LocalAuthorizer("local")
    authorizer.grant(SOURCE, {"document.index"})

    assert not authorizer.is_authorized("local", other, "source.access")
    assert not authorizer.is_authorized("local", other, "document.index")


def test_a_source_without_capabilities_is_still_accessible() -> None:
    authorizer = LocalAuthorizer("local")
    authorizer.grant(SOURCE, ())

    assert authorizer.is_authorized("local", SOURCE, "source.access")
    assert not authorizer.is_authorized("local", SOURCE, "document.index")


def test_regranting_replaces_the_version_and_capabilities() -> None:
    authorizer = LocalAuthorizer("local")
    authorizer.grant(SOURCE, {"document.index", "document.retrieve"})
    newer = SourceRef("doc-1", "text/markdown", "sha256:bbb")

    authorizer.grant(newer, {"document.index"})

    assert not authorizer.is_authorized("local", SOURCE, "source.access")
    assert authorizer.is_authorized("local", newer, "document.index")
    assert not authorizer.is_authorized("local", newer, "document.retrieve")


def test_revoking_removes_access_and_is_harmless_for_unknown_sources() -> None:
    authorizer = LocalAuthorizer("local")
    other = SourceRef("doc-2", "text/markdown", "sha256:aaa")
    authorizer.grant(SOURCE, {"document.index"})
    authorizer.grant(other, {"document.index"})

    authorizer.revoke("doc-1")
    authorizer.revoke("never-granted")

    assert not authorizer.is_authorized("local", SOURCE, "source.access")
    assert authorizer.is_authorized("local", other, "document.index")


def test_granted_capabilities_are_a_snapshot() -> None:
    authorizer = LocalAuthorizer("local")
    capabilities = ["document.index"]
    authorizer.grant(SOURCE, capabilities)

    capabilities.append("document.retrieve")

    assert not authorizer.is_authorized("local", SOURCE, "document.retrieve")


@pytest.mark.parametrize("tenant", ["", "   ", None, 7])
def test_authorizer_needs_a_tenant(tenant: object) -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        LocalAuthorizer(tenant)  # type: ignore[arg-type]


def test_registrar_protocols_describe_what_local_mode_needs_from_plugins() -> None:
    class Databases:
        def database_source_version(self, database_path: str) -> str:
            return "v1"

        def register_database_config(self, source: SourceRef, database_path: str, config: dict) -> None:
            return None

        def unregister_database(self, source_id: str) -> int:
            return 0

    assert isinstance(FakeDocumentsPlugin(), DocumentRegistrar)
    assert not isinstance(FakeDocumentsPlugin(), DatabaseRegistrar)
    assert isinstance(Databases(), DatabaseRegistrar)
    assert not isinstance(BasicExtension(documents_manifest()), DocumentRegistrar)
    assert not isinstance(object(), DocumentRegistrar)


# ---------------------------------------------------------------------------
# Submitted files


@pytest.mark.parametrize(
    ("filename", "source_type"),
    [
        ("a.md", "text/markdown"),
        ("a.markdown", "text/markdown"),
        ("a.txt", "text/plain"),
        ("a.text", "text/plain"),
        ("a.html", "text/html"),
        ("a.htm", "text/html"),
        ("A.MD", "text/markdown"),
        ("Report.Final.TXT", "text/plain"),
    ],
)
def test_document_suffixes_map_to_a_source_type_and_the_documents_plugin(
    filename: str, source_type: str
) -> None:
    assert classify_file(Path(filename)) == FileKind("document", source_type, "documents")


@pytest.mark.parametrize("filename", ["a.sqlite", "a.sqlite3", "a.db", "A.DB", "data.2024.SQLite3"])
def test_sqlite_suffixes_map_to_the_databases_plugin(filename: str) -> None:
    assert classify_file(Path(filename)) == FileKind(
        "database", "application/vnd.sqlite3", "databases"
    )


@pytest.mark.parametrize("filename", ["report.pdf", "REPORT.PDF"])
def test_pdf_files_are_refused_as_unsupported_in_this_version(filename: str) -> None:
    with pytest.raises(ConfigurationError, match="not supported in DataIndex v0.1.0"):
        classify_file(Path(filename))


@pytest.mark.parametrize(
    ("filename", "shown"),
    [
        ("data.xyz", "'.xyz'"),
        ("archive.tar.gz", "'.gz'"),
        ("image.png", "'.png'"),
        ("notes.md.exe", "'.exe'"),
        ("README", "(no extension)"),
        (".md", "(no extension)"),
    ],
)
def test_other_suffixes_are_refused_and_the_supported_ones_are_listed(
    filename: str, shown: str
) -> None:
    with pytest.raises(ConfigurationError, match="unsupported file type") as error:
        classify_file(Path(filename))

    assert shown in str(error.value)
    assert ".md" in str(error.value)
    assert ".sqlite3" in str(error.value)


def test_resolve_submitted_path_returns_the_absolute_path_of_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sub").mkdir()
    target = tmp_path / "a.md"
    target.write_text("# A\n", encoding="utf-8")

    assert resolve_submitted_path(str(target)) == target.resolve()
    assert resolve_submitted_path(target) == target.resolve()
    assert resolve_submitted_path(tmp_path / "sub" / ".." / "a.md") == target.resolve()
    monkeypatch.chdir(tmp_path)
    resolved = resolve_submitted_path("a.md")
    assert resolved == target.resolve()
    assert resolved.is_absolute()


def test_resolve_submitted_path_expands_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.md"
    target.write_text("# A\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert resolve_submitted_path("~/a.md") == target.resolve()


def test_resolve_submitted_path_accepts_any_path_like_object(tmp_path: Path) -> None:
    target = tmp_path / "a.md"
    target.write_text("# A\n", encoding="utf-8")

    class PathLike:
        def __fspath__(self) -> str:
            return str(target)

    assert resolve_submitted_path(PathLike()) == target.resolve()  # type: ignore[arg-type]


def test_resolve_submitted_path_requires_an_existing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_submitted_path(tmp_path / "missing.md")


def test_resolve_submitted_path_refuses_directories_without_echoing_the_path(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "folder.md"
    folder.mkdir()

    with pytest.raises(ConfigurationError, match="is not a regular file") as error:
        resolve_submitted_path(folder)

    assert "'folder.md'" in str(error.value)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("path", [b"a.md", 7, None, 1.5, ["a.md"]])
def test_resolve_submitted_path_rejects_non_paths(path: object) -> None:
    with pytest.raises(ValueError, match="path must be a str or os.PathLike"):
        resolve_submitted_path(path)  # type: ignore[arg-type]


def test_resolve_submitted_path_rejects_embedded_nul_characters() -> None:
    with pytest.raises((ValueError, OSError)):
        resolve_submitted_path("a\x00b.md")


def test_read_submitted_file_returns_the_raw_bytes(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    payload = "caf\u00e9".encode() + b" \xff\xfe binary"
    path.write_bytes(payload)

    assert read_submitted_file(path) == payload


def test_read_submitted_file_accepts_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")

    assert read_submitted_file(path) == b""


def test_read_submitted_file_accepts_exactly_the_size_limit(tmp_path: Path) -> None:
    path = tmp_path / "limit.txt"
    path.write_bytes(b"x" * MAX_SUBMITTED_FILE_BYTES)

    assert len(read_submitted_file(path)) == MAX_SUBMITTED_FILE_BYTES


def test_read_submitted_file_refuses_one_byte_over_the_limit(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_bytes(b"x" * (MAX_SUBMITTED_FILE_BYTES + 1))

    with pytest.raises(ConfigurationError, match="larger than the 5 MiB limit") as error:
        read_submitted_file(path)

    assert "'big.txt'" in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_read_submitted_file_of_a_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_submitted_file(tmp_path / "missing.txt")


def test_source_ids_are_stable_for_a_path_and_differ_between_paths(tmp_path: Path) -> None:
    first, second = tmp_path / "one" / "a.md", tmp_path / "two" / "a.md"

    assert source_id_for(first, "document") == source_id_for(first, "document")
    assert source_id_for(first, "document") != source_id_for(second, "document")
    assert source_id_for(first, "document") != source_id_for(tmp_path / "one" / "b.md", "document")
    assert source_id_for(first, "document") != source_id_for(first, "database")


@pytest.mark.parametrize(("kind", "prefix"), [("document", "doc-"), ("database", "db-")])
def test_source_ids_carry_a_kind_prefix_and_are_valid_workspace_ids(
    tmp_path: Path, kind: str, prefix: str
) -> None:
    source_id = source_id_for(tmp_path / "a.md", kind)

    assert re.fullmatch(re.escape(prefix) + "[0-9a-f]{16}", source_id)
    WorkspaceDocument(
        doc_id=source_id,
        name="a.md",
        kind=kind,
        source_type="text/markdown",
        plugin_id="documents",
        path=str(tmp_path / "a.md"),
        version="sha256:aaa",
    )


def test_source_ids_do_not_reveal_the_path(tmp_path: Path) -> None:
    source_id = source_id_for(tmp_path / "secret-plans.md", "document")

    assert "secret" not in source_id
    assert tmp_path.name not in source_id


@pytest.mark.skipif(os.name != "nt", reason="only Windows paths are case-insensitive")
def test_source_ids_ignore_case_on_case_insensitive_file_systems(tmp_path: Path) -> None:
    upper = Path(str(tmp_path).upper()) / "A.MD"
    lower = Path(str(tmp_path).lower()) / "a.md"

    assert source_id_for(upper, "document") == source_id_for(lower, "document")


def test_source_ids_survive_undecodable_file_names() -> None:
    source_id = source_id_for(Path("folder") / "bad\udcffname.md", "document")

    assert source_id.startswith("doc-")


def test_content_version_is_the_sha256_of_the_bytes() -> None:
    assert content_version(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert content_version(b"abc") == (
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert content_version(b"abc") != content_version(b"abd")


@pytest.mark.parametrize(
    ("plugin_id", "hint"),
    [
        ("documents", "pip install dataindex-ext-documents"),
        ("databases", "pip install dataindex-ext-databases"),
        ("premium-demo", "install the plugin and trust it"),
        ("", "install the plugin and trust it"),
    ],
)
def test_install_hint_names_the_package_for_first_party_plugins(
    plugin_id: str, hint: str
) -> None:
    assert install_hint(plugin_id) == hint
