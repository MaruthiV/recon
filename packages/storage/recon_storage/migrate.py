from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

HERE = Path(__file__).parent


def config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    return cfg


def upgrade(dsn: str, rev: str = "head") -> None:
    command.upgrade(config(dsn), rev)


if __name__ == "__main__":
    # python -m recon_storage.migrate revision "message" <owner dsn>
    _, cmd, msg, dsn = sys.argv
    assert cmd == "revision"
    command.revision(config(dsn), message=msg, autogenerate=True)
