"""
Pytest configuration for worker tests.

Worker tests import directly from app.tasks.* (the worker package).
pythonpath = ["."] in pyproject.toml means `app` resolves to apps/worker/app/.

Celery is configured in always-eager mode so tasks execute synchronously
in-process. With task_eager_propagates=True, self.retry() raises
celery.exceptions.Retry directly to the test, enabling pytest.raises(Retry)
assertions without a real broker.
"""
import pytest


@pytest.fixture(autouse=True)
def celery_eager_mode():
    """
    Force all Celery tasks to execute synchronously in-process for tests.
    task_eager_propagates=True ensures self.retry() surfaces as Retry exception.
    """
    from app.celery_app import celery_app
    original_always_eager = celery_app.conf.task_always_eager
    original_propagates = celery_app.conf.task_eager_propagates

    celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
    )
    yield
    celery_app.conf.update(
        task_always_eager=original_always_eager,
        task_eager_propagates=original_propagates,
    )