"""Compatibility alias for source checkouts importing ``am_common``."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_mail_commands import common

sys.modules[__name__] = common
