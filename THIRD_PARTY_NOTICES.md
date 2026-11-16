# Third-party notices

## PageIndex-derived hierarchy assembly

The `build_hierarchy` function in `packages/core/src/dataindex/hierarchy.py`
adapts the ordered-stack parent-assignment approach from
`pageindex/flash/outline/tree.py::build_tree` in Vectify AI's PageIndex repository.

- Upstream: <https://github.com/VectifyAI/PageIndex>
- Pinned commit: `2ac8694bf8fe89a3840dda9bdb88db5119b77fc4`
- Upstream copyright: `Copyright (c) 2025 Vectify AI`
- Transformation: removed PageIndex's PDF `Block`, heading parser, page ranges,
  dictionaries and empty-child pruning; the adapted helper accepts typed DataIndex
  nodes and explicit heading levels, assigns parent IDs, and preserves each native
  locator and source version.

The upstream MIT license text follows:

```text
MIT License

Copyright (c) 2025 Vectify AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Optional document parser dependencies

The `dataindex-ext-documents[pdf]` extra depends on `pypdf>=7.1.0,<8`
(BSD-3-Clause). The `legacy-office` extra depends on `olefile>=0.47,<1`
(BSD-2-Clause) and `xlrd>=2.0.2,<3` (BSD-style terms; xlrd's distribution also
contains an older 4-clause BSD license for earlier work). The project does not
copy or modify their source; these packages are installed as separate Python
distributions. Dependency versions and license files were checked from the
installed distributions used for format tests.

The older xlrd work requires this acknowledgment when redistributing that work:

> This product includes software developed by David Giffin <david@giffin.org>.

The corresponding license also requires retaining its acknowledgment in
redistributions. Consult each installed distribution's complete license file
before redistributing its code.

## Optional database driver dependencies

`dataindex-ext-databases` has no runtime dependency beyond `dataindex-core`.
Its extras install these drivers as separate Python distributions; the project
does not copy, modify or bundle their source. Licenses were read from the
installed distributions' metadata during development:

| Extra | Distributions | License |
| --- | --- | --- |
| every SQL server extra | SQLAlchemy | MIT |
| `duckdb` | duckdb | MIT |
| `postgresql` | psycopg, psycopg-binary | LGPL-3.0-only |
| `mysql` | PyMySQL; cryptography (`rsa` extra) | MIT; Apache-2.0 OR BSD-3-Clause |
| `mssql` | pyodbc | MIT-0 |
| `oracle` | oracledb | UPL-1.0 OR Apache-2.0 |
| `snowflake` | snowflake-sqlalchemy, snowflake-connector-python | Apache-2.0 |
| `bigquery` | sqlalchemy-bigquery | MIT |
| `clickhouse` | clickhouse-connect | Apache-2.0 |
| `mongodb` | pymongo | Apache-2.0 |

psycopg is LGPL-licensed. DataIndex imports it unmodified at run time and
anyone may replace it with another build. The psycopg-binary wheel bundles
libpq and its native dependencies under their own licenses. The `mssql` extra
also needs the Microsoft ODBC Driver for SQL Server, a separate system package
under Microsoft's license terms that DataIndex does not distribute. Transitive
dependencies of these drivers carry their own licenses; review them before
redistributing an environment.
