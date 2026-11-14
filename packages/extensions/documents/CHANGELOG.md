# Changelog

## 0.1.0

- Initial free DocumentIndex extension for Markdown, plain text and HTML.
- Added the `documents.get_section` tool (one section by outline `node_id`) and
  `unregister_document(source_id)`, which the local-mode SDK uses to read
  sections and to forget deleted documents.
- Parsers run in linear time on hostile input. An ATX heading followed by a long
  run of blanks, an unclosed `![` or `[x](` in a heading, thousands of nested
  ignored HTML elements and 100,000 HTML headings each took minutes to hours;
  each now finishes or is refused in seconds.
- Markdown heading titles are capped at 512 characters, and HTML with more than
  10,000 headings is refused before it is fully parsed.
- Line numbers count only `\n`, `\r` and `\r\n`, as editors do. Form feed, NEL,
  U+2028 and similar separators no longer shift cited line numbers or bypass the
  100,000-line limit.
