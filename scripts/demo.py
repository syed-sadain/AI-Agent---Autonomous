"""Run both test inputs against a running server and save the Word files.
Usage:  python scripts/demo.py [http://localhost:8000]
"""
import json
import sys
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo_output"
OUT.mkdir(exist_ok=True)

for name in ("standard", "complex"):
    body = json.loads((ROOT / "samples" / f"{name}_request.json").read_text())
    print(f"\n{'=' * 78}\n{name.upper()} REQUEST\n{body['request']}\n{'=' * 78}")
    r = httpx.post(f"{BASE}/agent", json=body, timeout=300)
    r.raise_for_status()
    d = r.json()
    print(f"status: {d['status']}   total: {d['total_ms']} ms   providers: {d['llm']['providers_used']}")
    print(f"\n{d['message']}\n\nAGENT-GENERATED TODO LIST (wave = parallel group):")
    for t in d["tasks"]:
        deps = ",".join(t["depends_on"]) or "-"
        print(f"  [{t['status']:<9}] wave {t['wave']}  {t['id']:<5} {t['tool']:<18} {t['title']}  (after: {deps})")
    if d["plan"]["assumptions"]:
        print("\nASSUMPTIONS:", *[f"\n  - {a}" for a in d["plan"]["assumptions"]])
    if d["plan"]["ambiguities"]:
        print("\nCONFLICTS / GAPS RESOLVED:", *[f"\n  - {a['issue']}\n      -> {a['decision']}" for a in d["plan"]["ambiguities"]])
    llm = d["llm"]
    print(f"\nLLM: {llm['calls']} calls, {llm['retries']} retries, {llm['provider_failures']} provider failures")
    doc = httpx.get(d["document"]["download_url"], timeout=60)
    path = OUT / f"{name}.docx"
    path.write_bytes(doc.content)
    print(f"saved -> {path} ({len(doc.content):,} bytes)")
