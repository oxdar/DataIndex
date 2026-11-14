# Future Phase 08 — VisualIndex & GeometryIndex FREE or freemium

**Not part of v0.1.0**; implement only after the first release passes all gates. Add `dataindex-ext-visual` and `dataindex-ext-geometry` as separate optional wheels.

### VisualIndex
- Start: JPG, PNG, TIFF (test each). Ingest EXIF metadata and image dimensions, OCR with optional Tesseract backend, manual annotations with exact pixel-region locators; vision model interpretation explicitly inferred.
- Use Pillow/OpenCV only inside extension. Never convert hallucinated captions into verified facts.
- Bounded region crop retrieval, permission check on original image, provenance with file version and bounding box.

### GeometryIndex
- Start: glTF/GLB, OBJ, STL (test each); parse scene hierarchy, nodes, transforms, materials, bounding boxes and units where defined.
- Use trimesh/Open3D as optional extension dependencies; large meshes remain stored in original assets.
- Geometry metrics are deterministic and must identify coordinate reference, units and validity.

### Acceptance
- Install each wheel independently; uninstall without breaking core or the other extension.
- Visual: OCR-labeled evidence distinguished from image model inference, region coordinates valid.
- Geometry: hierarchy, bounding box and measurement validated against fixture mesh.
- Zero embeddings and no image/3D dependencies in core.

## Copilot protocol
Read `AGENTS.md`, extension contract, security and packaging docs. Implement this phase only when explicitly authorized after `v0.1.0` release. Build an independent wheel with actual format fixtures, unit/integration/security tests, native evidence locators, and no core regression. Do not mark supported formats until tests pass.
