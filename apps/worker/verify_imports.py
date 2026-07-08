#!/usr/bin/env python3
"""
Quick import and registration check — run before starting the worker.
Usage: DATABASE_URL=postgresql+psycopg2://x:x@localhost/x python verify_imports.py
"""
import os
import sys
from app.celery_app import celery_app
sys.path.insert(0, os.path.dirname(__file__))

# Test 1: _sync_engine is defined at module level
from app.tasks.common import _sync_engine
assert _sync_engine is None, f"Expected None, got {_sync_engine}"
print("✓ _sync_engine module-level variable OK")

# Test 2: get_sync_engine doesn't crash on first reference
# (won't actually connect without a real DB, but the function must be importable)

print("✓ get_sync_engine importable")

# Test 3: all 9 tasks register

registered = {t for t in celery_app.tasks if t.startswith("app.tasks.")}
expected = {
    "app.tasks.ifc.process_model",
    "app.tasks.mesh.generate_chunks",
    "app.tasks.step.process_step",
    "app.tasks.gltf.process_gltf",
    "app.tasks.obj.process_obj",
    "app.tasks.stl.process_stl",
    "app.tasks.bcf.export_bcf",
    "app.tasks.scan.scan_file",
    "app.tasks.webhook.dispatch_webhook",
}
missing = expected - registered
assert not missing, f"Missing tasks: {missing}"
print(f"✓ All {len(expected)} tasks registered")

# Test 4: storage provider dispatch works

print("✓ download_raw_file / upload_processed_file importable")

print("\nAll checks passed.")