"""FastAPI application and OpenAI-compatible RCA facade."""

from .app import create_app
from .config import ApiConfig
from .service import RcaApiService
from .storage import RunStore

__all__ = ["ApiConfig", "RcaApiService", "RunStore", "create_app", "create_app_from_env"]


def __getattr__(name: str):
    if name == "create_app_from_env":
        from .server import create_app_from_env

        return create_app_from_env
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
