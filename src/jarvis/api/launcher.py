"""Run the HUD local-data API on localhost only."""

import uvicorn

from jarvis import config
from jarvis.api.main import app


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=config.API_PORT)


if __name__ == "__main__":
    main()
