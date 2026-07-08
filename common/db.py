"""Connection helper shared by every module. One env var, one place to read it."""
import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://lab:lab@localhost:5432/sysdesign"
)
