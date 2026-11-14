# Future Phase 11 — GeoIndex, RealityIndex and MediaIndex separately distributed

**Not part of v0.1.0.** These can be free/freemium/paid after customer validation; decide licenses per extension.

### GeoIndex
- GeoJSON, GeoPackage, shapefiles then GeoTIFF. Spatial CRS/vertical datums must be explicit; use GDAL/Rasterio/Shapely/pyproj in extension only.
- Tools: contains, intersects, nearest feature, bounded raster window. Never interpret degrees as meters.

### RealityIndex
- LAS/LAZ LiDAR, E57, PLY with compatible readers. Native point-cloud source version, bbox, CRS, classification, histograms and bounded spatial subset.
- Use indexed tiles/blocks for IO only—this is not embedding chunking.

### MediaIndex
- Audio/video metadata, keyframes, timestamps, transcripts via optional trusted ASR; preserve source timecode and model-generated confidence.
- Bound frame extraction and audio data; avoid storing excessive raw media in index.

### Acceptance
- Each module installs/uninstalls independently and passes native fixture tests.
- Spatial tests use known CRS/coordinates; timestamps are precise; transcriptions distinct from source truth.
- Provenance and permissions enforced across all modalities.

## Copilot protocol
Read `AGENTS.md`, extension contract, security and packaging docs. Implement this phase only when explicitly authorized after `v0.1.0` release. Build an independent wheel with actual format fixtures, unit/integration/security tests, native evidence locators, and no core regression. Do not mark supported formats until tests pass.
