"""Entry point so the container can ``python -m pubmed_mock``."""

from __future__ import annotations

import uvicorn
from pubmed_mock.main import app


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info")


if __name__ == "__main__":
    main()
