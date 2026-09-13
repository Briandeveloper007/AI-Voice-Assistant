# conftest.py — adds the harley root to sys.path so pytest can import
# commands.schema and security.permissions without installing the package.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
