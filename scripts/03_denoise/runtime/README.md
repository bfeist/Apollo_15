# Frozen denoise execution source

These files are the exact sources bound to the active reviewed denoise run and
qualified accelerator. `source_manifest.json` records their SHA-256 hashes and
original paths. The internal `scripts/` and `research/` structure is intentional:
the qualification code uses these relative paths when checking its provenance.

Run commands through `scripts/run.py` or the stage-03 console. The layout adapter
resolves external evidence and initializes imports in each worker process.
See [the layout guide](../../../docs/script_layout.md).
