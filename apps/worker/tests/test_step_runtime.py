"""
STEP runtime smoke tests.

These exist purely to catch a broken/missing OCCT binding at CI time,
before it reaches production as a silent "every STEP upload fails"
regression. They deliberately do NOT re-test STEP conversion logic —
that's covered by the existing converter pipeline tests. This file only
answers one question: is cadquery-ocp-novtk actually installed and
importable under the module names apps/worker/app/tasks/step.py expects?

If cadquery-ocp-novtk isn't installed in the environment running these
tests (e.g. a local dev machine without the full worker Docker image),
these tests are skipped rather than failed — they're a deployment-
correctness check, not a correctness check for step.py's own code.
"""
import importlib

import pytest


def _ocp_available() -> bool:
    try:
        importlib.import_module("OCP")
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(
    not _ocp_available(),
    reason="cadquery-ocp-novtk not installed in this environment — "
           "run inside the worker Docker image to exercise this check",
)


class TestOCPImportable:
    """Mirrors the Dockerfile build-time self-test, as a regular test too
    so it also runs under `pytest` in any environment that has the
    dependency installed (e.g. a future CI job running inside the worker
    image), not just at `docker build` time."""

    def test_ocp_submodules_importable(self):
        import OCP.STEPControl  # noqa: F401
        import OCP.IFSelect  # noqa: F401
        import OCP.BRepMesh  # noqa: F401
        import OCP.RWGltf  # noqa: F401
        import OCP.TDocStd  # noqa: F401
        import OCP.TCollection  # noqa: F401
        import OCP.XCAFDoc  # noqa: F401
        import OCP.XCAFApp  # noqa: F401
        import OCP.TDF  # noqa: F401
        import OCP.TopExp  # noqa: F401
        import OCP.TopAbs  # noqa: F401

    def test_step_control_reader_instantiable(self):
        """Beyond import success, confirm the specific classes step.py
        actually calls can be instantiated — catches an API-shape
        mismatch (e.g. a wrong constructor signature between
        pythonocc-core and OCP) that a bare import wouldn't."""
        from OCP.STEPControl import STEPControl_Reader
        reader = STEPControl_Reader()
        assert reader is not None


class TestStepPyDoesNotFallBackToRuntimeError:
    """
    Confirms apps/worker/app/tasks/step.py's own import-guard functions
    succeed now that the runtime dependency is present — i.e. the
    RuntimeError("OCCT Python bindings are not installed") fallback path
    is not hit. This does not test conversion correctness, only that the
    dependency-missing guard clause no longer triggers.
    """

    def test_read_step_import_guard_passes(self):
        from app.tasks.step import _read_step
        # Calling with a nonexistent path should fail with a file-not-found
        # style RuntimeError from OCCT itself (or a clear read failure),
        # NOT the "OCCT Python bindings are not installed" guard message —
        # that distinction is exactly what this test is checking.
        with pytest.raises(RuntimeError) as exc_info:
            _read_step("/nonexistent/path/does-not-exist.step")
        assert "not installed" not in str(exc_info.value)

    def test_tessellate_import_guard_passes(self):
        """
        _mesh_shape's OCP.BRepMesh import must succeed. We can't easily
        build a real TopoDS_Shape here without a valid STEP file, so this
        only exercises the import guard itself, not a full tessellation.
        """
        from OCP.BRepMesh import BRepMesh_IncrementalMesh  # noqa: F401
        # If the above import raised, app.tasks.step._mesh_shape's own
        # try/except would raise RuntimeError("...not installed") for
        # every real call — reaching this line proves that path is clear.