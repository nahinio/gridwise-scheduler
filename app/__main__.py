"""`python -m app` - one process, one worker, bound to 0.0.0.0:$PORT (default 8000)."""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=get_settings().port,
        workers=1,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
