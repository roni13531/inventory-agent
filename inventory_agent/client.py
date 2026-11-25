from __future__ import annotations

import os
from typing import Any


def _secret(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    try:
        from google.colab import userdata  # type: ignore[import-not-found]
        value = userdata.get(name)
    except Exception:
        value = None
    if not value:
        raise RuntimeError(f"Missing required secret {name}; set it in the environment or Colab Secrets.")
    return str(value)


def create_supabase_client() -> Any:
    """Create a sync client without logging either credential."""
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError("Install the pinned supabase package before creating a client.") from exc
    return create_client(_secret("SUPABASE_URL"), _secret("SUPABASE_KEY"))
