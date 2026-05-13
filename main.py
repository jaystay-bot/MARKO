"""MARKO Vercel/WSGI entrypoint - exposes Flask `app`."""
from flask import Flask
import dashboard

# Vercel's Flask framework detector statically parses this file and requires a
# literal top-level `app = Flask(...)` assignment. We satisfy that check, then
# immediately rebind `app` to dashboard's fully-configured Flask instance
# (which carries all the routes, templates, and view functions).
app = Flask(__name__)
app = dashboard.app
