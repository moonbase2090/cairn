"""Keep the unit suite off the machine-wide embed daemon."""
import os

os.environ.setdefault("CAIRN_EMBEDD", "0")
