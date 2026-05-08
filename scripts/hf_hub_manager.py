#!/usr/bin/env python3
"""
HF Inference Endpoint lifecycle manager for the Ambient Patient Companion.
Run from Replit Shell.

Usage:
  python scripts/hf_hub_manager.py status
  python scripts/hf_hub_manager.py list
  python scripts/hf_hub_manager.py update-adapters
  python scripts/hf_hub_manager.py set-adapter ORG/REPO[@REV] [...]
  python scripts/hf_hub_manager.py wake
  python scripts/hf_hub_manager.py scale-to-zero
  python scripts/hf_hub_manager.py pause
  python scripts/hf_hub_manager.py resume
  python scripts/hf_hub_manager.py delete
"""
import asyncio, asyncpg, os, sys
from huggingface_hub import get_inference_endpoint, list_inference_endpoints, InferenceEndpointStatus

TOKEN     = os.environ["HF_TOKEN"]
ADMIN_TOK = os.environ.get("HF_ENDPOINT_ADMIN_TOKEN", TOKEN)
NAME      = os.environ.get("HF_ENDPOINT_NAME", "companion-qwen25-3b")
NAMESPACE = os.environ.get("HF_NAMESPACE", "")
DB_URL    = os.environ.get("DATABASE_URL", "")

def get_ep():
    return get_inference_endpoint(NAME, namespace=NAMESPACE, token=ADMIN_TOK)

def cmd_status():
    ep = get_ep()
    env = ep.raw.get("model", {}).get("image", {}).get("custom", {}).get("env", {})
    print(f"Name:          {ep.name}")
    print(f"Status:        {ep.status}")
    print(f"URL:           {ep.url or '(not running)'}")
    print(f"LORA_ADAPTERS: {env.get('LORA_ADAPTERS', '(none)')}")

def cmd_list():
    for ep in list_inference_endpoints(namespace=NAMESPACE, token=ADMIN_TOK):
        print(f"  {ep.name:40s}  {str(ep.status):20s}  {ep.url or '(off)'}")

def cmd_update_adapters():
    async def _fetch():
        pool = await asyncpg.create_pool(DB_URL)
        rows = await pool.fetch(
            "SELECT hf_repo, hf_revision FROM slm_adapter_registry"
            " WHERE status='active' ORDER BY adapter_type, created_at"
        )
        await pool.close()
        return rows
    rows = asyncio.run(_fetch())
    lora_val = ",".join(f"{r['hf_repo']}@{r['hf_revision']}" for r in rows)
    print(f"Setting LORA_ADAPTERS = {lora_val or '(empty)'}")
    ep = get_ep()
    ep.update(custom_image={"env": {"LORA_ADAPTERS": lora_val}})
    print("Update sent. Endpoint restarting (~2-3 min).")

def cmd_set_adapter(specs):
    lora_val = ",".join(specs)
    get_ep().update(custom_image={"env": {"LORA_ADAPTERS": lora_val}})
    print(f"LORA_ADAPTERS = {lora_val}")

def cmd_wake():
    ep = get_ep()
    print(f"Current status: {ep.status}")
    if ep.status.value in ("scaledToZero", "paused"):
        ep.resume(); ep.wait(timeout=180)
    print(f"Status: {ep.status}  URL: {ep.url}")

def cmd_scale_to_zero():
    get_ep().scale_to_zero()
    print("Scaled to zero. Next request triggers cold start (~30-90s).")

def cmd_pause():
    if input("Pause? Will NOT auto-resume. [y/N]: ").lower() != "y": return
    get_ep().pause()
    print("Paused. Run: python scripts/hf_hub_manager.py resume")

def cmd_resume():
    ep = get_ep(); ep.resume(); ep.wait(timeout=180)
    print(f"Status: {ep.status}  URL: {ep.url}")

def cmd_delete():
    if input(f"DELETE '{NAME}'? Type DELETE to confirm: ") != "DELETE": return
    get_ep().delete(); print(f"Deleted '{NAME}'.")

CMDS = {
    "status": cmd_status, "list": cmd_list, "update-adapters": cmd_update_adapters,
    "wake": cmd_wake, "scale-to-zero": cmd_scale_to_zero,
    "pause": cmd_pause, "resume": cmd_resume, "delete": cmd_delete,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "set-adapter": cmd_set_adapter(sys.argv[2:])
    elif cmd in CMDS: CMDS[cmd]()
    else: print(f"Unknown: {cmd}. Options:", ", ".join(CMDS))
