import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Muss vor dem Import von app.main stehen: eine echte .env darf in Tests nie mitreden
# (load_env ueberschreibt gesetzte Variablen nicht).
TOKEN = "test-token-geheim-und-lang-genug-0123456789"
os.environ["BRIDGE_TOKEN"] = TOKEN
for key in (
    "CLAUDE_DEFAULT_MODEL",
    "CLAUDE_DEFAULT_EFFORT",
    "CLAUDE_DEFAULT_ULTRACODE",
    "CLAUDE_WORKDIR",
    "CLAUDE_BIN",
    "CLAUDE_MAX_SESSIONS",
    "CLAUDE_IDLE_HOURS",
):
    os.environ[key] = ""
