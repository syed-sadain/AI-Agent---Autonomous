import os
import tempfile

# Must be set BEFORE app modules import get_settings(): tests never touch the network.
os.environ["LLM_PROVIDERS"] = "mock"
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="agent-tests-")
