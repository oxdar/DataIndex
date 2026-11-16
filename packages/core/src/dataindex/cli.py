"""Secure-by-default local command-line interface for DataIndex."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from dataindex.catalog import SQLiteCatalog
from dataindex.cli_local import (
    COMMAND_ERRORS,
    LOCAL_COMMANDS,
    add_local_commands,
    printable,
    run_local_command,
)
from dataindex.exceptions import (
    SourceAuthorizationError,
    ToolNotAvailableError,
)
from dataindex.local import (
    BINARY_DOCUMENT_SUFFIXES,
    DOCUMENT_SUFFIXES,
    MAX_SUBMITTED_BINARY_FILE_BYTES,
    MAX_SUBMITTED_FILE_BYTES,
)
from dataindex.models import EvidenceRecord, SourceRef, ToolRequest
from dataindex.reasoning import PlannedToolCall
from dataindex.registry import ExtensionRegistry
from dataindex.sdk import DataIndexClient

MAX_DOCUMENT_BYTES = MAX_SUBMITTED_FILE_BYTES
MAX_BINARY_DOCUMENT_BYTES = MAX_SUBMITTED_BINARY_FILE_BYTES
DOCUMENT_SOURCE_TYPES = tuple(dict.fromkeys(DOCUMENT_SUFFIXES.values()))
_BINARY_SOURCE_TYPES = frozenset(
    DOCUMENT_SUFFIXES[suffix] for suffix in BINARY_DOCUMENT_SUFFIXES
)


class _LocalAuthorizer:
    def __init__(self, tenant_id: str, source_id: str) -> None:
        self._tenant_id = tenant_id
        self._source_id = source_id

    def is_authorized(
        self, tenant_id: str, source: SourceRef, capability: str
    ) -> bool:
        return (
            tenant_id == self._tenant_id
            and source.source_id == self._source_id
            and capability
            in {"source.access", "document.index", "document.retrieve"}
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "extensions":
            return _extensions(args)
        if args.command == "sources":
            return _sources(args)
        if args.command == "nodes":
            return _nodes(args)
        if args.command in {"index", "query"}:
            return _document_operation(args)
        if args.command in LOCAL_COMMANDS:
            return run_local_command(args)
    except COMMAND_ERRORS as error:
        print(f"dataindex: {printable(str(error))}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dataindex",
        description=(
            "DataIndex: vectorless, cited retrieval over documents and databases."
        ),
        epilog=(
            "quick start: dataindex submit report.md; "
            "dataindex chat 'What are the payment terms?' --model gpt-4o"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add_local_commands(commands)

    extension_parser = commands.add_parser("extensions")
    extension_commands = extension_parser.add_subparsers(
        dest="extension_command", required=True
    )
    extension_commands.add_parser("list")
    inspect_parser = extension_commands.add_parser("inspect")
    inspect_parser.add_argument("plugin_id")

    source_parser = commands.add_parser("sources")
    source_commands = source_parser.add_subparsers(
        dest="source_command", required=True
    )
    source_list = source_commands.add_parser("list")
    _add_catalog_scope(source_list)

    node_parser = commands.add_parser("nodes")
    node_commands = node_parser.add_subparsers(dest="node_command", required=True)
    node_search = node_commands.add_parser("search")
    _add_catalog_scope(node_search)
    node_search.add_argument("--source-id", required=True)
    node_search.add_argument("--text", required=True)
    node_search.add_argument("--limit", type=int, default=25)

    for command in ("index", "query"):
        document_parser = commands.add_parser(command)
        _add_catalog_scope(document_parser)
        document_parser.add_argument("--source-id", required=True)
        document_parser.add_argument(
            "--source-type",
            required=True,
            choices=DOCUMENT_SOURCE_TYPES,
        )
        document_parser.add_argument("--file", required=True, type=Path)
        document_parser.add_argument("--trusted-distribution", required=True)
        document_parser.add_argument("--trusted-version", required=True)
        document_parser.add_argument("--enable-plugin", action="store_true")
        if command == "query":
            document_parser.add_argument("--text", required=True)
    return parser


def _add_catalog_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog", type=Path, default=Path("dataindex.sqlite"))
    parser.add_argument("--tenant", required=True)


def _extensions(args: argparse.Namespace) -> int:
    statuses = ExtensionRegistry().discover()
    if args.extension_command == "inspect":
        status = next(
            (item for item in statuses if item.plugin_id == args.plugin_id), None
        )
        if status is None:
            print(f"Extension {args.plugin_id!r} is not installed.", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "plugin_id": status.plugin_id,
                    "distribution": status.distribution,
                    "distribution_version": status.distribution_version,
                    "installed": status.installed,
                    "discovered": status.discovered,
                    "compatible": status.compatible,
                    "trusted": status.trusted,
                    "enabled": status.enabled,
                    "entitled": status.entitled,
                    "tenant_authorized": status.tenant_authorized,
                    "healthy": status.healthy,
                    "error": status.error,
                },
                sort_keys=True,
            )
        )
        return 0
    if not statuses:
        print("No extensions discovered.")
        return 0
    for status in statuses:
        fields = (
            f"installed={status.installed}",
            f"discovered={status.discovered}",
            f"compatible={status.compatible}",
            f"trusted={status.trusted}",
            f"enabled={status.enabled}",
            f"entitled={_display(status.entitled)}",
            f"tenant_authorized={_display(status.tenant_authorized)}",
            f"healthy={_display(status.healthy)}",
        )
        suffix = f" error={status.error}" if status.error else ""
        print(f"{status.plugin_id}: " + " ".join(fields) + suffix)
    return 0


def _sources(args: argparse.Namespace) -> int:
    with SQLiteCatalog(args.catalog) as catalog:
        print(
            json.dumps(
                [
                    {
                        "source_id": source.source_id,
                        "source_type": source.source_type,
                        "version": source.version,
                    }
                    for source in catalog.list_sources(args.tenant)
                ],
                sort_keys=True,
            )
        )
    return 0


def _nodes(args: argparse.Namespace) -> int:
    with SQLiteCatalog(args.catalog) as catalog:
        if catalog.get_source_tenant(args.source_id) != args.tenant:
            raise SourceAuthorizationError(
                f"source {args.source_id!r} does not belong to tenant {args.tenant!r}"
            )
        nodes = catalog.search_nodes(
            args.text, source_id=args.source_id, limit=args.limit
        )
        print(
            json.dumps(
                [
                    {
                        "node_id": node.node_id,
                        "kind": node.kind,
                        "title": node.title,
                        "source_id": node.source_id,
                        "source_version": node.source_version,
                        "locator": {
                            "scheme": node.locator.scheme,
                            "version": node.locator.locator_version,
                            "value": node.locator.value,
                        },
                    }
                    for node in nodes
                ],
                sort_keys=True,
            )
        )
    return 0


def _document_operation(args: argparse.Namespace) -> int:
    if not args.enable_plugin:
        raise ValueError("pass --enable-plugin to explicitly enable documents")
    content = _read_document(args.file, args.source_type)
    source = SourceRef(
        source_id=args.source_id,
        source_type=args.source_type,
        version=f"sha256:{hashlib.sha256(content).hexdigest()}",
    )
    with DataIndexClient.open(
        args.catalog,
        tenant_id=args.tenant,
        source_authorizer=_LocalAuthorizer(args.tenant, args.source_id),
        trusted_distributions=((args.trusted_distribution, args.trusted_version),),
        enabled_plugin_ids=("documents",),
        deployment="local-cli",
    ) as client:
        extension = client.extension("documents")
        from dataindex_ext_documents import DocumentIndexExtension

        if not isinstance(extension, DocumentIndexExtension):
            raise ToolNotAvailableError("the loaded plugin is not the documents plugin")
        extension.register_document(source, content)
        if args.command == "index":
            client.register_source(source)
            nodes = client.index(source, "documents")
            print(
                json.dumps(
                    {
                        "source_id": source.source_id,
                        "source_version": source.version,
                        "indexed_records": len(nodes),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if client.get_source(source.source_id) != source:
            raise ValueError(
                "source is unavailable or file content differs; run dataindex index first"
            )
        call = PlannedToolCall(
            plugin_id="documents",
            source=source,
            request=ToolRequest(
                tool_id="documents.search",
                parameters={
                    "source_id": source.source_id,
                    "source_version": source.version,
                    "query": args.text,
                },
            ),
        )
        evidence = client.retrieve(call, maximum_results=1)
        print(json.dumps(_evidence_json(evidence), sort_keys=True))
    return 0


def _read_document(path: Path, source_type: str) -> bytes:
    limit = (
        MAX_BINARY_DOCUMENT_BYTES
        if source_type in _BINARY_SOURCE_TYPES
        else MAX_DOCUMENT_BYTES
    )
    with path.open("rb") as document:
        content = document.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"document exceeds the {limit}-byte CLI limit")
    if not content:
        raise ValueError("document is empty")
    return content


def _evidence_json(evidence: EvidenceRecord) -> dict[str, object]:
    return {
        "source_id": evidence.source_id,
        "source_version": evidence.source_version,
        "operation": evidence.operation,
        "value": evidence.value,
        "execution_id": evidence.execution_id,
        "node_id": evidence.node_id,
        "locator": {
            "scheme": evidence.locator.scheme,
            "version": evidence.locator.locator_version,
            "value": evidence.locator.value,
        },
    }


def _display(value: bool | None) -> str:
    if value is None:
        return "not_checked"
    return str(value).lower()
