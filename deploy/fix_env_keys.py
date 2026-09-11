from pathlib import Path

p = Path("/opt/snapforget/.env")
out = []
for line in p.read_text(encoding="utf-8").splitlines(True):
    stripped = line.lstrip()
    if stripped and not stripped.startswith("#") and "=" in line:
        key, sep, value = line.partition("=")
        line = key.strip().replace("-", "_").upper() + sep + value
    out.append(line)
p.write_text("".join(out), encoding="utf-8")
print("ok")
