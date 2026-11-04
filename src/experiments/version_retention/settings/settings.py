import os

SECRET_KEY = os.environ["PULP_SECRET_KEY"]
CONTENT_ORIGIN = f"http://localhost:{os.environ.get('PULP_PORT', '5001')}"
ANALYTICS = False
