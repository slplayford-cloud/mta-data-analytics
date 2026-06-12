#!/usr/bin/env python3
"""Launch the MTA subway map server.

Required environment variables:
  SUPABASE_URL         — your Supabase project URL
  SUPABASE_SERVICE_KEY — service-role key (server-side only)

Usage:
  python3 run_server.py
  .venv/bin/python run_server.py
"""
import uvicorn

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    uvicorn.run(
        "src.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
