"""Local launcher for the FastAPI API service.

Run this file directly from PyCharm to start the app without manually typing
the uvicorn command.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


ROOT_DIR = Path(__file__).resolve().parent
ENV_PATH = ROOT_DIR.parent / ".env"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.envfile import load_env_file


if __name__ == "__main__":
    os.chdir(ROOT_DIR)
    load_env_file(ENV_PATH)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8077)
