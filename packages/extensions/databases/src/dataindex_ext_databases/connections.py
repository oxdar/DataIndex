"""Validated, secret-free descriptions of database server connections.

A connection is described by a URL **without a password**, an optional
``password_env`` naming the environment variable that holds the password, and
an optional schema. The canonical form returned by ``parse_connection`` is safe
to persist and to show: it never contains a secret. The password is read from
the environment only at the moment a connection is opened.

Each engine accepts an explicit allowlist of URL query parameters. Anything
else is refused, because driver options such as MySQL ``init_command`` and
``local_infile``, SQL Server ``odbc_connect`` or BigQuery inline credentials
could run statements, read local files or replace the reviewed connection.

Parsing uses only the standard library, so the base extension still installs
without any database driver.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote

from dataindex_ext_databases.exceptions import (
    DatabaseConnectionError,
    DatabaseDriverUnavailableError,
    InvalidDatabaseConnectionError,
)

MAX_URL_CHARS = 2048
MAX_QUERY_VALUE_CHARS = 512
MAX_NAME_CHARS = 128
MAX_DATABASE_PATH_CHARS = 256
MAX_ERROR_CHARS = 300
CONNECTION_SOURCE_TYPE_PREFIX = "application/vnd.dataindex.db."
CONNECTION_KEYS = frozenset({"engine", "url", "password_env", "schema"})
PASSWORD_ENV_PATTERN = re.compile(r"DATAINDEX_[A-Za-z0-9_]{1,118}\Z")
TIER_INTEGRATION = "integration"
TIER_EXPERIMENTAL = "experimental"

_HOST = re.compile(r"[A-Za-z0-9_.-]{1,253}\Z")
_IPV6 = re.compile(r"\[[0-9A-Fa-f:.]{2,45}\]\Z")
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,63}\Z")
_FILE_SCHEMES = {"sqlite": "SQLite", "duckdb": "DuckDB"}


@dataclass(frozen=True, slots=True)
class EngineSpec:
    """One supported server engine and its safety rules."""

    name: str
    label: str
    family: str
    drivers: Mapping[str, tuple[str, ...]]
    aliases: Mapping[str, str]
    extra: str
    query_parameters: frozenset[str]
    tier: str
    password: bool = True
    schema: bool = True
    multiple_hosts: bool = False
    host_required: bool = True
    database_required: bool = False
    database_segments: int = 1
    required_parameters: frozenset[str] = frozenset()
    fixed_parameters: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @property
    def source_type(self) -> str:
        return CONNECTION_SOURCE_TYPE_PREFIX + self.name


def _engine(**values: object) -> EngineSpec:
    return EngineSpec(**values)  # type: ignore[arg-type]


ENGINES: dict[str, EngineSpec] = {
    spec.name: spec
    for spec in (
        _engine(
            name="postgresql",
            label="PostgreSQL",
            family="sqlalchemy",
            drivers={
                "postgresql+psycopg": ("sqlalchemy", "psycopg"),
                "postgresql+psycopg2": ("sqlalchemy", "psycopg2"),
                "postgresql+pg8000": ("sqlalchemy", "pg8000"),
            },
            aliases={"postgresql": "postgresql+psycopg", "postgres": "postgresql+psycopg"},
            extra="postgresql",
            query_parameters=frozenset(
                {
                    "sslmode",
                    "sslrootcert",
                    "sslcert",
                    "sslkey",
                    "application_name",
                    "target_session_attrs",
                }
            ),
            tier=TIER_INTEGRATION,
        ),
        _engine(
            name="mysql",
            label="MySQL",
            family="sqlalchemy",
            drivers={
                "mysql+pymysql": ("sqlalchemy", "pymysql"),
                "mysql+mysqldb": ("sqlalchemy", "MySQLdb"),
                "mysql+mysqlconnector": ("sqlalchemy", "mysql.connector"),
            },
            aliases={"mysql": "mysql+pymysql"},
            extra="mysql",
            query_parameters=frozenset(
                {"charset", "ssl_ca", "ssl_check_hostname", "ssl_verify_cert"}
            ),
            tier=TIER_INTEGRATION,
        ),
        _engine(
            name="mariadb",
            label="MariaDB",
            family="sqlalchemy",
            drivers={
                "mariadb+pymysql": ("sqlalchemy", "pymysql"),
                "mariadb+mysqldb": ("sqlalchemy", "MySQLdb"),
                "mariadb+mariadbconnector": ("sqlalchemy", "mariadb"),
            },
            aliases={"mariadb": "mariadb+pymysql"},
            extra="mysql",
            query_parameters=frozenset(
                {"charset", "ssl_ca", "ssl_check_hostname", "ssl_verify_cert"}
            ),
            tier=TIER_INTEGRATION,
        ),
        _engine(
            name="mssql",
            label="SQL Server",
            family="sqlalchemy",
            drivers={
                "mssql+pyodbc": ("sqlalchemy", "pyodbc"),
                "mssql+pymssql": ("sqlalchemy", "pymssql"),
            },
            aliases={"mssql": "mssql+pyodbc", "sqlserver": "mssql+pyodbc"},
            extra="mssql",
            query_parameters=frozenset(
                {
                    "driver",
                    "Encrypt",
                    "TrustServerCertificate",
                    "ApplicationIntent",
                    "Trusted_Connection",
                }
            ),
            tier=TIER_INTEGRATION,
            fixed_parameters={"mssql+pyodbc": {"ApplicationIntent": "ReadOnly"}},
        ),
        _engine(
            name="oracle",
            label="Oracle Database",
            family="sqlalchemy",
            drivers={"oracle+oracledb": ("sqlalchemy", "oracledb")},
            aliases={"oracle": "oracle+oracledb"},
            extra="oracle",
            query_parameters=frozenset({"service_name"}),
            tier=TIER_INTEGRATION,
        ),
        _engine(
            name="snowflake",
            label="Snowflake",
            family="sqlalchemy",
            drivers={"snowflake": ("sqlalchemy", "snowflake.sqlalchemy")},
            aliases={},
            extra="snowflake",
            query_parameters=frozenset({"warehouse", "role"}),
            tier=TIER_EXPERIMENTAL,
            database_segments=2,
        ),
        _engine(
            name="bigquery",
            label="BigQuery",
            family="sqlalchemy",
            drivers={"bigquery": ("sqlalchemy", "sqlalchemy_bigquery")},
            aliases={},
            extra="bigquery",
            query_parameters=frozenset({"location", "maximum_bytes_billed"}),
            tier=TIER_EXPERIMENTAL,
            password=False,
            schema=False,
            host_required=False,
            database_required=True,
            required_parameters=frozenset({"maximum_bytes_billed"}),
        ),
        _engine(
            name="clickhouse",
            label="ClickHouse",
            family="sqlalchemy",
            drivers={"clickhousedb": ("sqlalchemy", "clickhouse_connect")},
            aliases={"clickhouse": "clickhousedb"},
            extra="clickhouse",
            query_parameters=frozenset({"secure", "verify"}),
            tier=TIER_EXPERIMENTAL,
            schema=False,
        ),
        _engine(
            name="mongodb",
            label="MongoDB",
            family="mongodb",
            drivers={
                "mongodb": ("pymongo",),
                "mongodb+srv": ("pymongo",),
            },
            aliases={},
            extra="mongodb",
            query_parameters=frozenset(
                {
                    "authSource",
                    "authMechanism",
                    "tls",
                    "tlsCAFile",
                    "replicaSet",
                    "readPreference",
                    "directConnection",
                    "appName",
                }
            ),
            tier=TIER_INTEGRATION,
            schema=False,
            multiple_hosts=True,
            database_required=True,
        ),
    )
}


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    """A validated connection. ``url`` is canonical and holds no password."""

    engine: EngineSpec
    scheme: str
    url: str
    username: str | None
    hosts: tuple[tuple[str, int | None], ...]
    database: str | None
    query: tuple[tuple[str, str], ...]
    password_env: str | None
    schema: str | None

    def to_mapping(self) -> dict[str, str | None]:
        return {
            "engine": self.engine.name,
            "url": self.url,
            "password_env": self.password_env,
            "schema": self.schema,
        }

    @property
    def connection_id(self) -> str:
        """A digest that names the server, database and schema, not credentials."""
        identity = json.dumps(
            {"url": self.url, "schema": self.schema},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    @property
    def display_name(self) -> str:
        host = self.hosts[0][0] if self.hosts else ""
        target = "/".join(part for part in (host, self.database or "") if part)
        if self.schema:
            target = f"{target}.{self.schema}" if target else self.schema
        return f"{self.engine.label} {target or self.engine.name}"[:256]

    def require_driver(self) -> None:
        """Fail with an install hint when this URL's driver is not importable."""
        missing = [
            module for module in self.engine.drivers[self.scheme] if not _importable(module)
        ]
        if missing:
            raise DatabaseDriverUnavailableError(
                f"{self.engine.label} support needs {', '.join(missing)}; run: "
                f'pip install "dataindex-ext-databases[{self.engine.extra}]"'
            )

    def password(self) -> str | None:
        """Read the password from its environment variable at connection time."""
        if self.password_env is None:
            return None
        value = os.environ.get(self.password_env)
        if not value:
            raise DatabaseConnectionError(
                f"set the {self.password_env} environment variable to the password "
                f"for {self.url}"
            )
        return value

    def connection_error(
        self, action: str, error: BaseException, password: str | None
    ) -> DatabaseConnectionError:
        """A connection error whose message names only the redacted URL."""
        return DatabaseConnectionError(
            f"could not {action} {self.url}: {scrub(error, password)}"
        )


def parse_connection(connection: Mapping[str, object]) -> ConnectionSettings:
    """Validate a connection mapping and return its canonical settings.

    ``connection`` holds ``url`` and optionally ``password_env``, ``schema`` and
    ``engine``. When ``engine`` is present it must match the URL. No network
    access happens here.
    """
    if not isinstance(connection, Mapping) or not all(
        isinstance(key, str) for key in connection
    ):
        raise InvalidDatabaseConnectionError("a connection must be an object")
    unknown = sorted(set(connection) - CONNECTION_KEYS)
    if unknown:
        raise InvalidDatabaseConnectionError(
            f"unsupported connection fields: {unknown}"
        )
    url = connection.get("url")
    if not isinstance(url, str) or not url:
        raise InvalidDatabaseConnectionError("a connection needs a url string")
    if len(url) > MAX_URL_CHARS:
        raise InvalidDatabaseConnectionError(
            f"connection URLs must be at most {MAX_URL_CHARS} characters"
        )
    if any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise InvalidDatabaseConnectionError(
            "connection URLs must not contain spaces or control characters; "
            "percent-encode them"
        )
    if "\\" in url or "#" in url:
        raise InvalidDatabaseConnectionError(
            "connection URLs must not contain backslashes or fragments"
        )
    raw_scheme, separator, remainder = url.partition("://")
    scheme = raw_scheme.lower()
    if not separator or not _SCHEME.fullmatch(scheme):
        raise InvalidDatabaseConnectionError(
            "a connection URL looks like engine://user@host:port/database"
        )
    engine, scheme = _engine_for(scheme)

    authority, slash, rest = remainder.partition("/")
    if not slash:
        authority, _, query_text = authority.partition("?")
        path = None
    else:
        path, _, query_text = rest.partition("?")
    username, hosts = _authority(engine, authority)
    database = _database(engine, path)
    query = _query(engine, scheme, query_text)
    password_env = _password_env(engine, connection.get("password_env"))
    schema = _schema(engine, connection.get("schema"))

    canonical = _canonical_url(scheme, username, hosts, database, query)
    declared = connection.get("engine")
    if declared is not None and declared != engine.name:
        raise InvalidDatabaseConnectionError(
            f"the connection engine {declared!r} does not match its URL"
        )
    return ConnectionSettings(
        engine=engine,
        scheme=scheme,
        url=canonical,
        username=username,
        hosts=hosts,
        database=database,
        query=query,
        password_env=password_env,
        schema=schema,
    )


def supported_engines() -> dict[str, dict[str, object]]:
    """Engine names with their URL schemes, install extra and support tier."""
    return {
        name: {
            "label": spec.label,
            "schemes": sorted(set(spec.drivers) | set(spec.aliases)),
            "extra": spec.extra,
            "tier": spec.tier,
        }
        for name, spec in sorted(ENGINES.items())
    }


def scrub(error: BaseException | str, password: str | None) -> str:
    """A short single-line message with every form of ``password`` removed."""
    text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    if password:
        for form in {password, quote(password, safe=""), quote(password)}:
            text = text.replace(form, "***")
    text = " ".join(text.split())
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 3] + "..."
    return text


def _engine_for(scheme: str) -> tuple[EngineSpec, str]:
    base = scheme.split("+", 1)[0]
    if base in _FILE_SCHEMES:
        raise InvalidDatabaseConnectionError(
            f"{_FILE_SCHEMES[base]} databases are files; submit the file by path "
            "instead of a URL"
        )
    for spec in ENGINES.values():
        if scheme in spec.drivers:
            return spec, scheme
        if scheme in spec.aliases:
            return spec, spec.aliases[scheme]
    for spec in ENGINES.values():
        if base in spec.aliases or any(
            name.split("+", 1)[0] == base for name in spec.drivers
        ):
            accepted = ", ".join(sorted(set(spec.drivers) | set(spec.aliases)))
            raise InvalidDatabaseConnectionError(
                f"unsupported {spec.label} driver {scheme!r}; use one of: {accepted}"
            )
    raise InvalidDatabaseConnectionError(
        f"unsupported database URL scheme {scheme!r}; supported engines: "
        + ", ".join(sorted(ENGINES))
    )


def _authority(
    engine: EngineSpec, authority: str
) -> tuple[str | None, tuple[tuple[str, int | None], ...]]:
    userinfo, at, host_text = authority.rpartition("@")
    username: str | None = None
    if at:
        if ":" in userinfo:
            raise InvalidDatabaseConnectionError(
                "connection URLs must not contain a password; store it in an "
                "environment variable named DATAINDEX_... and pass password_env"
            )
        username = unquote(userinfo)
        if not username or len(username) > MAX_NAME_CHARS or _has_control(username):
            raise InvalidDatabaseConnectionError(
                f"the user name must be 1 to {MAX_NAME_CHARS} printable characters"
            )
        if not engine.password:
            raise InvalidDatabaseConnectionError(
                f"{engine.label} connections authenticate with the environment's "
                "default credentials; remove the user name"
            )
    if not host_text:
        if engine.host_required:
            raise InvalidDatabaseConnectionError(
                f"{engine.label} connection URLs need a host"
            )
        return username, ()
    parts = host_text.split(",")
    if len(parts) > 1 and not engine.multiple_hosts:
        raise InvalidDatabaseConnectionError(
            f"{engine.label} connection URLs name exactly one host"
        )
    if len(parts) > 16:
        raise InvalidDatabaseConnectionError("a connection may name at most 16 hosts")
    return username, tuple(_host(part) for part in parts)


def _host(text: str) -> tuple[str, int | None]:
    if text.startswith("["):
        closing = text.find("]")
        host, rest = text[: closing + 1], text[closing + 1 :]
        if closing < 0 or not _IPV6.fullmatch(host):
            raise InvalidDatabaseConnectionError(f"invalid IPv6 host {text!r}")
        port_text = rest[1:] if rest.startswith(":") else None
        if rest and port_text is None:
            raise InvalidDatabaseConnectionError(f"invalid host {text!r}")
    else:
        host, colon, port_text_value = text.partition(":")
        port_text = port_text_value if colon else None
        if not _HOST.fullmatch(host):
            raise InvalidDatabaseConnectionError(f"invalid host {text!r}")
    port: int | None = None
    if port_text is not None:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise InvalidDatabaseConnectionError(f"invalid port in {text!r}")
        port = int(port_text)
    return host, port


def _database(engine: EngineSpec, path: str | None) -> str | None:
    if path is None:
        if engine.database_required:
            raise InvalidDatabaseConnectionError(
                f"{engine.label} connection URLs need a database name"
            )
        return None
    database = unquote(path)
    if engine.database_required and not database:
        raise InvalidDatabaseConnectionError(
            f"{engine.label} connection URLs need a database name"
        )
    if len(database) > MAX_DATABASE_PATH_CHARS or _has_control(database):
        raise InvalidDatabaseConnectionError(
            f"the database name must be at most {MAX_DATABASE_PATH_CHARS} "
            "printable characters"
        )
    segments = database.split("/")
    if len(segments) > engine.database_segments or (
        database and any(not segment for segment in segments)
    ):
        raise InvalidDatabaseConnectionError(
            f"{engine.label} connection URLs name the database as "
            + ("/database/schema" if engine.database_segments == 2 else "/database")
        )
    return database


def _query(
    engine: EngineSpec, scheme: str, text: str
) -> tuple[tuple[str, str], ...]:
    try:
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=bool(text))
    except ValueError:
        raise InvalidDatabaseConnectionError(
            "the connection URL query string is malformed"
        ) from None
    allowed = {name.casefold(): name for name in engine.query_parameters}
    values: dict[str, str] = {}
    for key, value in pairs:
        canonical = allowed.get(key.casefold())
        if canonical is None:
            accepted = ", ".join(sorted(engine.query_parameters)) or "none"
            raise InvalidDatabaseConnectionError(
                f"URL parameter {key[:64]!r} is not allowed for {engine.label}; "
                f"allowed: {accepted}"
            )
        if canonical in values:
            raise InvalidDatabaseConnectionError(
                f"URL parameter {canonical!r} is repeated"
            )
        if len(value) > MAX_QUERY_VALUE_CHARS or _has_control(value):
            raise InvalidDatabaseConnectionError(
                f"URL parameter {canonical!r} must be at most "
                f"{MAX_QUERY_VALUE_CHARS} printable characters"
            )
        values[canonical] = value
    for name, fixed in engine.fixed_parameters.get(scheme, {}).items():
        current = values.get(name)
        if current is not None and current.casefold() != fixed.casefold():
            raise InvalidDatabaseConnectionError(
                f"{engine.label} connections require {name}={fixed}"
            )
        values[name] = fixed
    missing = sorted(engine.required_parameters - set(values))
    if missing:
        raise InvalidDatabaseConnectionError(
            f"{engine.label} connection URLs must set {', '.join(missing)}"
        )
    if "maximum_bytes_billed" in values and (
        not values["maximum_bytes_billed"].isdigit()
        or int(values["maximum_bytes_billed"]) < 1
    ):
        raise InvalidDatabaseConnectionError(
            "maximum_bytes_billed must be a positive whole number of bytes"
        )
    return tuple(sorted(values.items()))


def _password_env(engine: EngineSpec, value: object) -> str | None:
    if value is None:
        return None
    if not engine.password:
        raise InvalidDatabaseConnectionError(
            f"{engine.label} connections do not take a password"
        )
    if not isinstance(value, str) or not PASSWORD_ENV_PATTERN.fullmatch(value):
        raise InvalidDatabaseConnectionError(
            "password_env must name an environment variable that starts with "
            "DATAINDEX_ and uses only letters, digits and underscores"
        )
    return value


def _schema(engine: EngineSpec, value: object) -> str | None:
    if value is None:
        return None
    if not engine.schema:
        raise InvalidDatabaseConnectionError(
            f"{engine.label} connections do not take a separate schema"
        )
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_NAME_CHARS
        or _has_control(value)
    ):
        raise InvalidDatabaseConnectionError(
            f"schema must be 1 to {MAX_NAME_CHARS} printable characters"
        )
    return value


def _canonical_url(
    scheme: str,
    username: str | None,
    hosts: tuple[tuple[str, int | None], ...],
    database: str | None,
    query: tuple[tuple[str, str], ...],
) -> str:
    userinfo = f"{quote(username, safe='')}@" if username else ""
    host_text = ",".join(
        host if port is None else f"{host}:{port}" for host, port in hosts
    )
    path = "" if database is None else "/" + quote(database, safe="/")
    query_text = "&".join(
        f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in query
    )
    return f"{scheme}://{userinfo}{host_text}{path}" + (
        f"?{query_text}" if query_text else ""
    )


def _has_control(text: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in text)


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False
