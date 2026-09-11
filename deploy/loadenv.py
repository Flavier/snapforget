import base64
from pathlib import Path

src = Path("/tmp/e.b64")
dst = Path("/opt/snapforget/.env")
raw = "".join(src.read_text(encoding="ascii").split())
dst.write_bytes(base64.b64decode(raw))
print("ok")
