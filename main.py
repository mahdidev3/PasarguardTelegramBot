"""Application entrypoint.

Run the bot with:
    python main.py

Phase 0 keeps the current working bot implementation in app.legacy_bot so the
project can be tested without changing runtime behavior. Future phases will move
features from legacy_bot into dedicated routers/services/repositories.
"""

import asyncio

from app.legacy_bot import main


if __name__ == "__main__":
    asyncio.run(main())
