"""Connection helper shared by every module. One env var, one place to read it."""
import os

# Every process (API and worker) imports this module, so it's the one chokepoint to teach
# backend/.env to the environment before any secret is read. Env-first, so compose/Railway
# values always win and prod (no .env on disk) is a no-op. This is what makes GROQ/ANTHROPIC/
# LANGSMITH keys work locally from backend/.env without a per-key loader.
from common.env import load_local_env

load_local_env()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://lab:lab@localhost:5432/sysdesign"
)
