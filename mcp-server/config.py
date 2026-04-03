"""Configuration module — reads all settings from environment variables."""

import os

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
DATA_TRACK: str = os.environ.get("DATA_TRACK", "synthea")
SYNTHEA_OUTPUT_DIR: str = os.environ.get("SYNTHEA_OUTPUT_DIR", "/home/runner/synthea-output")
