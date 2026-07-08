# apps/api/app/core/metrics.py
"""
Prometheus metric definitions for the OpenFormat API.

All metrics are module-level singletons. Import this module wherever
you need to observe a metric — do NOT construct new Counter/Histogram
objects in routers or services.

Metrics exposed at GET /metrics (added in main.py).
"""

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# HTTP request metrics (also tracked by instrumentator, kept for custom labels)
# ---------------------------------------------------------------------------

# HTTP_REQUEST_DURATION = Histogram(
#     "http_request_duration_seconds",
#     "HTTP request latency by route and method",
#     ["method", "route", "status_code"],
#     buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
# )

# ACTIVE_REQUESTS = Gauge(
#     "http_active_requests",
#     "Number of HTTP requests currently being processed",
# )

# ---------------------------------------------------------------------------
# Upload pipeline metrics
# ---------------------------------------------------------------------------

UPLOAD_INITIATED_TOTAL = Counter(
    "upload_initiated_total",
    "Total upload initiations (POST /models/upload)",
)

UPLOAD_CONFIRMED_TOTAL = Counter(
    "upload_confirmed_total",
    "Total upload confirmations (POST /models/{id}/confirm)",
    ["result"],  # labels: "success", "size_mismatch", "mime_error", "not_found"
)

UPLOAD_DURATION = Histogram(
    "upload_confirm_duration_seconds",
    "Time to execute confirm_upload() including storage verification and task dispatch",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
)

UPLOAD_SIZE_BYTES = Histogram(
    "upload_file_size_bytes",
    "Size of confirmed uploads in bytes",
    buckets=[
        1_024, 10_240, 102_400, 1_048_576,
        10_485_760, 104_857_600, 524_288_000,
    ],
)

# ---------------------------------------------------------------------------
# Annotation metrics
# ---------------------------------------------------------------------------

ANNOTATION_CREATED_TOTAL = Counter(
    "annotation_created_total",
    "Total annotations created",
)

ANNOTATION_DURATION = Histogram(
    "annotation_create_duration_seconds",
    "Time to create an annotation including DB write and event broadcast",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)

# ---------------------------------------------------------------------------
# Auth metrics
# ---------------------------------------------------------------------------

AUTH_DURATION = Histogram(
    "auth_duration_seconds",
    "Time for auth operations (login, register, token refresh)",
    ["operation"],  # "login", "register", "refresh"
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1],
)

AUTH_FAILURE_TOTAL = Counter(
    "auth_failure_total",
    "Total authentication failures",
    ["reason"],  # "invalid_credentials", "invalid_token", "expired_token"
)

# ---------------------------------------------------------------------------
# Model status metrics
# ---------------------------------------------------------------------------

PROCESSING_MODELS = Gauge(
    "processing_models_total",
    "Models currently in 'processing' state (approximate — read from DB at scrape time)",
)

FAILED_MODELS_TOTAL = Counter(
    "failed_models_total",
    "Cumulative count of models that reached 'failed' status",
    ["reason"],  # "scan_infected", "conversion_error", "timeout", "size_exceeded"
)

MODEL_CONFIRM_DURATION = Histogram(
    "model_confirm_duration_seconds",
    "End-to-end duration of confirm_upload()",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2],
)