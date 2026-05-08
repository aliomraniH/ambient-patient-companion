"""Replit Scheduled Deployment — 08:00 UTC — wakes HF endpoint from scale-to-zero."""
import os
from huggingface_hub import get_inference_endpoint

ep = get_inference_endpoint(
    os.environ.get("HF_ENDPOINT_NAME", "companion-qwen25-3b"),
    namespace=os.environ.get("HF_NAMESPACE", ""),
    token=os.environ["HF_TOKEN"],
)
if ep.status.value in ("scaledToZero", "paused"):
    ep.resume()
    ep.wait(timeout=180)
    print(f"Endpoint warmed up. Status: {ep.status}  URL: {ep.url}")
else:
    print(f"Endpoint already running. Status: {ep.status}")
