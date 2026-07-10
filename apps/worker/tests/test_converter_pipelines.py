import pytest
from unittest.mock import MagicMock, patch
from app.tasks.ifc import process_model as process_ifc
from app.tasks.step import process_step
from app.tasks.gltf import process_gltf
from app.tasks.obj import process_obj
from app.tasks.stl import process_stl

@pytest.mark.asyncio
async def test_converter_pipeline_stubs():
    """Verify that all core converter pipelines are callable."""
    # This verifies the registration of the tasks and imports
    assert callable(process_ifc)
    assert callable(process_step)
    assert callable(process_gltf)
    assert callable(process_obj)
    assert callable(process_stl)

@pytest.mark.asyncio
@patch("app.tasks.common.get_model_row")
async def test_ifc_pipeline_handles_missing_model(mock_get_row):
    mock_get_row.return_value = None
    result = process_ifc(model_id="00000000-0000-0000-0000-000000000000")
    assert result["error"] == "model_not_found"