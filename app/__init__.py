import os
import sys

# lets every app.* module "import config" / "import smbio" / "import stash" etc. without repeating this hack itself
sys.path.insert(0, "/app/sorter")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sorter"))
