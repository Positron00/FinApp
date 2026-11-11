"""An app for test_serve (not a test module)."""

from fastapi import FastAPI

app = FastAPI()
app.state.shutdown_timeout_s = 12.5
