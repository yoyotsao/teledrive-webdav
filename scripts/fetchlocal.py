"""Explorer's "儲存在本地" verb runs this by absolute path, from any cwd."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from teledrive_webdav.windows.fetchlocal import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
