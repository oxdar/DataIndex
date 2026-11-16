# dataindex-ext-documents

An independently installable free DataIndex extension for common text, office,
data, email and e-book documents. Readers preserve native structure and return
typed sections with native locators; retrieval returns observed source-backed
evidence. The extension does not embed documents, create vector indexes or
split them into fixed-size token windows.

## Supported formats

| Extensions | Format | Source type | Parser |
|---|---|---|---|
| `.md`, `.markdown` | Markdown | `text/markdown` | Standard library |
| `.txt`, `.text`, `.rst`, `.log`, `.yaml`, `.yml`, `.toml`, `.ini`, `.cfg` | Plain text | `text/plain` | Standard library |
| `.html`, `.htm`, `.xhtml` | HTML | `text/html` | Standard library |
| `.pdf` | PDF | `application/pdf` | Optional `pypdf` |
| `.doc` | Word 97-2003 | `application/msword` | Optional `olefile` |
| `.docx` | Word Open XML | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | Standard library |
| `.xls` | Excel 97-2003 | `application/vnd.ms-excel` | Optional `xlrd` and `olefile` |
| `.xlsx` | Excel Open XML | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | Standard library |
| `.ppt` | PowerPoint 97-2003 | `application/vnd.ms-powerpoint` | Optional `olefile` |
| `.pptx` | PowerPoint Open XML | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | Standard library |
| `.odt` | OpenDocument text | `application/vnd.oasis.opendocument.text` | Standard library |
| `.ods` | OpenDocument spreadsheet | `application/vnd.oasis.opendocument.spreadsheet` | Standard library |
| `.odp` | OpenDocument presentation | `application/vnd.oasis.opendocument.presentation` | Standard library |
| `.rtf` | Rich Text Format | `application/rtf` | Standard library |
| `.epub` | EPUB | `application/epub+zip` | Standard library |
| `.eml` | Email | `message/rfc822` | Standard library |
| `.csv`, `.tsv` | Delimited tables | `text/csv`, `text/tab-separated-values` | Standard library |
| `.json`, `.jsonl`, `.ndjson` | JSON and JSON Lines | `application/json`, `application/x-ndjson` | Standard library |
| `.xml` | XML | `application/xml` | Standard library |

This is a tested format list, not a claim of universal document support. Scanned
PDF OCR, encrypted files whose keys are unavailable, and unlisted formats are
not supported. Spreadsheet formulas are not executed.

## Install

Install the matching core API and extension wheel independently:

```powershell
python -m pip install dataindex-core
python -m pip install dataindex-ext-documents
```

The core extension has no parser dependencies. Optional readers are installed
separately, and a missing parser reports an installation hint without disabling
other document formats:

```powershell
python -m pip install "dataindex-ext-documents[pdf]"
python -m pip install "dataindex-ext-documents[legacy-office]"
python -m pip install "dataindex-ext-documents[all]"
```

## Source handling, evidence and limits

The host authorizes and reads each source, then passes explicit `bytes` or UTF-8
`str` to `DocumentIndexExtension.register_document`. The extension does not
interpret source IDs as paths and performs no filesystem or network reads.
Documents are held in memory by the extension instance; the core catalog stores
typed nodes, locators and evidence. `unregister_document(source_id)` drops all
registered revisions for that source.

`documents.search` returns one best-matching section. `documents.get_section`
returns the section identified by an outline `node_id`. Both operations return
evidence with source ID, version, native locator and retrieval operation; an
unknown node ID raises `DocumentSectionNotFoundError`.

Text formats are limited to 5 MiB; binary formats to 50 MiB. The readers also
bound extracted text to 5 MiB, natural sections to 10,000, text lines to
100,000, archive members and inflated data, markup events, MIME parts, CSV rows,
and individual PDF work. The PDF reader caps each decoded stream at 32 MiB,
aggregate decoded streams at 128 MiB, pages at 2,000 and retained outline entries
at 2,000. Its deterministic extraction budget is
`min(32 MiB, 4 MiB + 16 * input_bytes)`. Dense or structurally expensive PDFs can take tens of seconds to be refused.
At roughly 8 microseconds per work unit on a slow test machine, the budget
ceiling can take about 4.5 minutes to reach. Split such PDFs into smaller files.

The PDF reader installs a process-wide `pypdf` decode hook, active only while a
DataIndex PDF is being parsed, to account for and memoize decoded streams.
This is an in-process parser guard, not a sandbox: only explicitly trusted
extension packages should be loaded. PDF extraction limits can also reject
unusually complex legitimate files.

HTML is extracted as text; script, style, template, iframe, object, SVG, MathML,
head and noscript content is omitted. HTML and Markdown are not executed or
rendered. Locator line numbers count only `\n`, `\r` and `\r\n`, as editors do;
Markdown heading titles are capped at 512 characters.

## Example

See [`examples/basic_usage.py`](examples/basic_usage.py). It reads the bundled
fixture in the host, passes the bytes explicitly, indexes natural sections and
prints the evidence source ID, version, locator and retrieval operation.

## License status

The public core/free-extension license has not yet been approved. The included
[`LICENSE`](LICENSE) file explicitly marks this distribution as not licensed for
redistribution until maintainer/legal review selects a license. No release is
implied by local wheel builds.
