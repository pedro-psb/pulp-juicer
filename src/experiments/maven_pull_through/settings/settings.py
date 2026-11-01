import json
import os

SECRET_KEY = os.environ["PULP_SECRET_KEY"]
CONTENT_ORIGIN = f"http://localhost:{os.environ.get('PULP_PORT', '5001')}"
ANALYTICS = False

for name, value in json.loads(
    os.environ.get("JUICER_PULP_SETTINGS_JSON", "{}")
).items():
    if not name.isidentifier() or not name.isupper():
        raise ValueError(f"Invalid candidate Pulp setting name: {name!r}")
    globals()[name] = value
