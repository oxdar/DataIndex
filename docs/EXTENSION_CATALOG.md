# Proposed extension catalog (roadmap, not implemented)

The public core exposes one interface for free and commercial extensions. Price and tier decisions are **hypotheses**, not validated customer demand. No extension listed here is currently a published product.

| Extension | v0.1 status | Proposed initial tier | First meaningful operation | Typical libraries / concerns |
|---|---|---|---|---|
| DocumentIndex | Required to build | FREE | Tested native-structure readers for text, office, PDF, data, email and e-book formats; cited retrieval | Standard library by default; optional pypdf, olefile and xlrd extras |
| DataBaseIndex | Required to build | FREE | Schema indexing and bounded approved queries for SQLite and DuckDB files and PostgreSQL, MySQL, MariaDB, SQL Server, Oracle and MongoDB servers; Snowflake, BigQuery and ClickHouse experimental | Standard library for SQLite; optional SQLAlchemy Core, duckdb, pymongo and per-engine driver extras; DB-native read-only logins |
| VisualIndex Basic | Future | FREE | Metadata/OCR and region locators | Pillow; OCR dependencies optional |
| GeometryIndex Basic | Future | FREE | glTF/OBJ nodes and bbox | trimesh; unit accuracy |
| BIMIndex Pro | Future | PAID | IFC element GUID/properties/containment | IfcOpenShell; legal/packaging review |
| CADIndex Pro | Future | PAID | STEP assemblies, precise geometry | Open Cascade, ezdxf; proprietary format limitations |
| SubsurfaceIndex Pro | Future | PAID | LAS/DLIS logs, SEG-Y, grids metadata and native bounded tools | lasio, dlisio, segyio, XTGeo, resqpy; specialized checks |
| RealityIndex Pro | Future | PAID | Point-cloud extents/classification | laspy, PDAL; CRS and massive IO |
| GeoIndex | Future | FREE or PAID TBD | GeoJSON/GeoPackage spatial search | GDAL, Shapely, pyproj |
| MediaIndex | Future | FREE or PAID TBD | Timecodes/transcript search | FFmpeg/ASR; model quality |
| Enterprise Connectors | Future | PAID | Enterprise source integration with auditing | Vendor API licenses/approval |

Commercialization requires a specific customer value proposition and pricing validation. The core must not depend on premium extensions. Extension tiers can be revised without rewriting core.
