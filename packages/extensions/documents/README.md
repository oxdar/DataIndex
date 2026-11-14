# dataindex-ext-documents

An independently installable free DataIndex extension for Markdown (`text/markdown`),
plain text (`text/plain`) and HTML (`text/html`). It preserves heading or paragraph
boundaries, emits versioned native line/heading locators, and returns one
deterministically selected section as source-backed evidence. Ranking uses
whole-word matches in each section's own text; cited evidence retains the full
selected section, including nested content where applicable.

## Install

Install the matching core API and this wheel independently:

```powershell
python -m pip install dataindex-core
python -m pip install dataindex-ext-documents
```

The extension adds no parser dependencies. It uses Python's standard-library
`html.parser` and does not execute HTML or Markdown content. PDF, OCR, file
watching, database formats, embeddings and token-window splitting are not
implemented.

## Source handling and limits

The host is responsible for authorization and reads the source; it passes
explicit `bytes` or UTF-8 `str` to `DocumentIndexExtension.register_document`.
The extension does not interpret source IDs as file paths and performs no
filesystem or network reads. Documents are held in memory by the extension
instance; the core catalog stores only typed nodes, locators and evidence.
`unregister_document(source_id)` drops every registered revision of a source and
returns how many were dropped.

The extension publishes two tools. `documents.search` returns the one best
matching section of a registered revision. `documents.get_section` returns the
full text of one section chosen by a `node_id` from the document outline, which
is how `DataIndexClient.get_section` and the agent tools read a section. Both
carry the `document.retrieve` capability and return evidence with the source
ID, version, native locator and operation; an unknown `node_id` raises
`DocumentSectionNotFoundError`.

The maximum input is 5 MiB and the maximum index is 10,000 natural sections.
Retrieval accepts a query of at most 256 characters and returns one best match
per call. HTML script, style, template, iframe, object, SVG, MathML, head and
noscript contents are omitted; this is a text extractor, not a browser renderer
or sanitization service.

Locator line numbers count only `\n`, `\r` and `\r\n`, as editors do, and a
document is limited to 100,000 lines; a trailing newline counts as one more line
for that limit. Markdown heading titles keep at most 512 characters, and parsing
is linear in the input size, so a crafted file is refused or indexed in seconds
rather than hanging the host. Python's `html.parser` still costs roughly 10-50
microseconds per tag, so a 5 MiB file of tiny tags can take tens of seconds.

## Example

See [`examples/basic_usage.py`](examples/basic_usage.py). It reads the bundled
fixture in the host, passes the bytes explicitly, indexes natural sections and
prints the evidence source ID, version, locator and retrieval operation.

## License status

The public core/free-extension license has not yet been approved. The included
[`LICENSE`](LICENSE) file explicitly marks this distribution as not licensed for
redistribution until maintainer/legal review selects a license. No release is
implied by local wheel builds.
