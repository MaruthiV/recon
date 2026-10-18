from google.adk.models import Gemini
from google.genai import types


def live_model(name: str) -> Gemini:
    # free tier has a per-minute cap too, wait it out instead of failing the run
    return Gemini(
        model=name,
        retry_options=types.HttpRetryOptions(attempts=3, initial_delay=10, max_delay=30, http_status_codes=[429, 503]),
    )
