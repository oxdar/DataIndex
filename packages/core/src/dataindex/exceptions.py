"""Explicit errors exposed by the DataIndex core."""


class DataIndexError(Exception):
    """Base class for expected DataIndex failures."""


class PluginManifestError(DataIndexError):
    """An extension manifest is missing, malformed or inconsistent."""


class DuplicatePluginError(DataIndexError):
    """More than one entry point declares the same plugin ID."""


class IncompatiblePluginError(DataIndexError):
    """An extension does not support the running core API."""


class UntrustedPluginError(DataIndexError):
    """An extension is not present in the administrator trust allowlist."""


class PluginLoadError(DataIndexError):
    """A trusted extension failed to load, initialize or report health."""


class CatalogError(DataIndexError):
    """Base class for persistent catalog failures."""


class CatalogCorruptionError(CatalogError):
    """Persisted catalog data is malformed or internally inconsistent."""


class CatalogIntegrityError(CatalogError):
    """A catalog operation would violate an index invariant."""


class UnsupportedSchemaVersionError(CatalogError):
    """The catalog schema is newer than, or cannot migrate to, this core."""


class DuplicateSourceError(CatalogIntegrityError):
    """A source ID is already registered."""


class SourceNotFoundError(CatalogError):
    """A requested source is not registered."""


class StaleSourceVersionError(CatalogIntegrityError):
    """A node or evidence record refers to a non-current source revision."""


class DuplicateNodeError(CatalogIntegrityError):
    """A node ID is already present in the catalog."""


class NodeNotFoundError(CatalogError):
    """A requested node or parent is not present in the active catalog."""


class InvalidEvidenceError(CatalogIntegrityError):
    """Evidence cannot be verified against its source and locator."""


class CrossTenantRelationshipError(CatalogIntegrityError):
    """A graph relationship crosses the catalog's tenant boundary."""


class ReasoningError(DataIndexError):
    """Base class for bounded reasoning and tool-dispatch failures."""


class SourceAuthorizationError(ReasoningError):
    """A tenant is not authorized to use a source."""


class EntitlementDeniedError(ReasoningError):
    """A required commercial capability is not entitled."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ToolNotAvailableError(ReasoningError):
    """A requested tool is not loaded, trusted, enabled or compatible."""


class InvalidToolRequestError(ReasoningError):
    """A planned tool request does not match its declared input schema."""


class InvalidToolEvidenceError(ReasoningError):
    """Tool evidence is not safely attributable to the requested source."""


class ToolEvidenceTooLargeError(InvalidToolEvidenceError):
    """A tool returned more evidence than the dispatcher accepts."""


class ToolExecutionError(ReasoningError):
    """A retrieval tool raised while running; the original error is the cause.

    ``error_type`` is the class name of the original error. It is the only part
    of the failure that is ever shown to a chat model.
    """

    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class ReasoningBudgetExceededError(ReasoningError):
    """The reasoning run exceeded its configured depth, call, token or time cap."""


class InvalidPlannerOutputError(ReasoningError):
    """A planner returned a malformed or unsupported turn."""


class InvalidCitationError(ReasoningError):
    """A claim cites missing or untrusted evidence."""


class ConfigurationError(DataIndexError):
    """A client, workspace or CLI option is missing, invalid or unsupported."""


class WorkspaceError(DataIndexError):
    """The local workspace registry is unreadable, corrupt or from a newer core."""


class DocumentNotFoundError(DataIndexError):
    """A document ID has not been submitted to this workspace."""


class SourceFileChangedError(DataIndexError):
    """A submitted file changed or disappeared after it was indexed."""


class SourceRejectedError(DataIndexError):
    """A plugin refused a submitted file, for example because it is malformed.

    ``error_type`` is the plugin exception's class name; the plugin's message is
    part of this error's text so the person who submitted the file can fix it.
    """

    def __init__(self, message: str, *, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class ChatModelError(DataIndexError):
    """A chat model backend failed or returned an unusable response."""


class ChatModelConfigurationError(ChatModelError):
    """A chat model is missing, misconfigured or its optional package is absent."""
