PageIndex-derived hierarchy assembly
====================================

The `build_hierarchy` function adapts the ordered-stack parent-assignment
approach from `pageindex/flash/outline/tree.py::build_tree` in Vectify AI's
PageIndex repository.

Upstream: https://github.com/VectifyAI/PageIndex
Commit: 2ac8694bf8fe89a3840dda9bdb88db5119b77fc4
Copyright: Copyright (c) 2025 Vectify AI

The adaptation removes PageIndex's PDF Block, heading parser, page ranges,
dictionaries and empty-child pruning. It accepts typed DataIndex nodes and
explicit levels, assigns parent IDs, and preserves native locators and source
versions.

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
