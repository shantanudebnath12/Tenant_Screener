"""Run Alembic migrations against DATABASE_URL.

    python scripts/migrate.py           # upgrade head
    python scripts/migrate.py downgrade # downgrade -1
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from alembic import command
from alembic.config import Config


def main() -> None:
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    action = sys.argv[1] if len(sys.argv) > 1 else "upgrade"
    if action == "upgrade":
        command.upgrade(cfg, "head")
    elif action == "downgrade":
        command.downgrade(cfg, "-1")
    elif action == "current":
        command.current(cfg)
    else:
        raise SystemExit(f"Unknown action: {action}")


if __name__ == "__main__":
    main()
