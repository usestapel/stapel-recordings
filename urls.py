"""Root URLconf for stapel-recordings — v1 canon mount (api-versioning.md §2, §6).

Canon: ``/<mod>/api/v1/...`` — the version segment sits right after ``api/``.
The host mounts ``include('stapel_recordings.urls')`` under ``recordings/``;
this module contributes the ``api/v1/`` prefix (the ``api/`` segment
historically lives inside this package, not in the host mount). The actual
URL set lives in ``urls_v1.py``; ``GATE_REGISTRY`` is re-exported here.
"""
from django.urls import include, path

from stapel_recordings.urls_v1 import GATE_REGISTRY  # noqa: F401  (re-export)

urlpatterns = [
    path('api/v1/', include('stapel_recordings.urls_v1')),
]
