"""Create or complete .env from .env.example, generating random secrets.

Usage: python scripts/make_env.py [OPENROUTER_KEY]
An existing .env is never rewritten: only missing variables are appended.
"""

import secrets
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
key = sys.argv[1] if len(sys.argv) > 1 else ""

generated = {
    "MONGO_ROOT_PASSWORD": secrets.token_urlsafe(24),
    "MONGO_APP_PASSWORD": secrets.token_urlsafe(24),
    "SESSION_SECRET": secrets.token_urlsafe(48),
    "BOOTSTRAP_ADMIN_PASSWORD": secrets.token_urlsafe(12),
    "OPENROUTER_API_KEY": key,
}

existing_lines = target.read_text().splitlines() if target.exists() else []
existing = {line.split("=", 1)[0].strip() for line in existing_lines if "=" in line and not line.startswith("#")}
if "OPENROUTER_KEY" in existing:  # accepted alias; don't add an empty duplicate that would shadow it
    existing.add("OPENROUTER_API_KEY")

added = []
for line in (root / ".env.example").read_text().splitlines():
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, example_value = line.split("=", 1)
    if name not in existing:
        added.append(f"{name}={generated.get(name, example_value)}")

if not added:
    sys.exit(".env is already complete.")
if target.exists():
    shutil.copy2(target, target.with_name(".env.bak"))
with target.open("a") as out:
    if existing_lines and existing_lines[-1].strip():
        out.write("\n")
    out.write("# added by scripts/make_env.py\n" + "\n".join(added) + "\n")
target.chmod(0o600)
print(f"{target}: added {', '.join(a.split('=')[0] for a in added)}")
if any(a.startswith("BOOTSTRAP_ADMIN_PASSWORD=") for a in added):
    print(f"Admin login: admin@example.com / {generated['BOOTSTRAP_ADMIN_PASSWORD']}")
