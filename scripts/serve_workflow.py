#!/usr/bin/env python3
"""One-file HTML UI for a deterministic workflow: paste input, watch the live
step log, get the final artifact + cost ledger.

Usage:
  python3 serve_workflow.py steps.yaml --input-file idea.md [--output publish] [--port 8787]

--output names the step whose artifact is shown as the result (default: the
last step). Each Run creates an isolated session dir under ui-sessions/ with
the workflow yaml copied in and the shared cache/ symlinked (repeat inputs are
near-free). Stdlib only; single page; no external assets.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

RUNNER = Path(__file__).parent / "run_steps.py"
SESSIONS: dict[str, dict] = {}
CFG: dict = {}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>
body{{margin:0;background:#0A0A0B;color:#EDEDED;font:15px/1.5 -apple-system,Inter,sans-serif;
display:grid;grid-template-columns:minmax(320px,1fr) 1.4fr;gap:0;height:100vh}}
.col{{padding:24px;overflow:auto}} .left{{border-right:1px solid rgba(255,255,255,.08)}}
h1{{font-size:17px;margin:0 0 2px}} .sub{{color:#A1A1AA;font-size:13px;margin:0 0 16px}}
textarea{{width:100%;height:40vh;background:#141417;color:#EDEDED;border:1px solid rgba(255,255,255,.12);
border-radius:8px;padding:12px;font:13px/1.5 ui-monospace,monospace;resize:vertical;box-sizing:border-box}}
button{{margin-top:12px;background:#5E6AD2;color:#fff;border:0;border-radius:8px;padding:10px 22px;
font-weight:600;font-size:14px;cursor:pointer}} button:disabled{{opacity:.45;cursor:default}}
pre{{background:#141417;border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:14px;
font:12px/1.55 ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}}
#log{{max-height:38vh;overflow:auto;color:#A1A1AA}} #out{{color:#EDEDED}}
.badge{{display:inline-block;font:600 11px/1 ui-monospace,monospace;letter-spacing:.08em;padding:4px 8px;
border-radius:4px;margin-left:8px}} .run{{background:#3b3b6e}} .ok{{background:#1e5c3a}} .bad{{background:#6e2f2f}}
table{{border-collapse:collapse;font:12px ui-monospace,monospace;margin-top:8px}}
td,th{{border:1px solid rgba(255,255,255,.1);padding:4px 10px;text-align:right}} td:first-child{{text-align:left}}
</style></head><body>
<div class="col left"><h1>{title}<span id="badge" class="badge" style="display:none"></span></h1>
<p class="sub">{nsteps} steps &middot; deterministic runner &middot; output: {outstep}</p>
<textarea id="input" placeholder="Paste the input for {inputfile}..."></textarea><br>
<button id="go" onclick="run()">Run workflow</button></div>
<div class="col"><h1>Log</h1><pre id="log">idle</pre><h1>Result</h1><pre id="out">&mdash;</pre><div id="ledger"></div></div>
<script>
let sid=null, timer=null;
function badge(t,c){{const b=document.getElementById('badge');b.textContent=t;b.className='badge '+c;b.style.display='inline-block';}}
async function run(){{
  const content=document.getElementById('input').value; if(!content.trim())return;
  document.getElementById('go').disabled=true; badge('RUNNING','run');
  document.getElementById('out').textContent='\\u2014'; document.getElementById('ledger').innerHTML='';
  const r=await fetch('/run',{{method:'POST',body:JSON.stringify({{content}})}}); sid=(await r.json()).session;
  timer=setInterval(poll,1500);
}}
async function poll(){{
  const s=await (await fetch('/status?session='+sid)).json();
  document.getElementById('log').textContent=s.log||'starting...';
  const lg=document.getElementById('log'); lg.scrollTop=lg.scrollHeight;
  if(s.done){{clearInterval(timer); document.getElementById('go').disabled=false;
    badge(s.exit===0?'PASS':'FAIL', s.exit===0?'ok':'bad');
    document.getElementById('out').textContent=s.output||'(no output artifact)';
    if(s.ledger)document.getElementById('ledger').innerHTML=s.ledger;}}
}}
</script></body></html>"""


def start_run(content: str) -> str:
    sid = uuid.uuid4().hex[:10]
    work = CFG["root"] / "ui-sessions" / f"{datetime.datetime.now().strftime('%m%d-%H%M%S')}-{sid}"
    work.mkdir(parents=True)
    work.joinpath(CFG["yaml_name"]).write_text(CFG["yaml_text"])
    work.joinpath(CFG["input_file"]).write_text(content)
    cache = CFG["root"] / "cache"
    cache.mkdir(exist_ok=True)
    (work / "cache").symlink_to(cache)
    proc = subprocess.Popen([sys.executable, str(RUNNER), CFG["yaml_name"]],
                            cwd=work, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    SESSIONS[sid] = {"work": work, "proc": proc, "lines": []}

    def pump():
        for line in proc.stdout:
            SESSIONS[sid]["lines"].append(line)
        proc.wait()
    threading.Thread(target=pump, daemon=True).start()
    return sid


def status(sid: str) -> dict:
    s = SESSIONS.get(sid)
    if not s:
        return {"done": True, "exit": 1, "log": "unknown session"}
    done = s["proc"].poll() is not None
    out = {"done": done, "exit": s["proc"].returncode, "log": "".join(s["lines"])[-20000:]}
    if done:
        runs = sorted(s["work"].glob("runs/*/"))
        if runs:
            artifact = runs[-1] / f"{CFG['output_step']}.md"
            out["output"] = artifact.read_text()[-30000:] if artifact.exists() else None
            ledger_file = runs[-1] / "ledger.json"
            if ledger_file.exists():
                rows = json.loads(ledger_file.read_text())
                cells = "".join(
                    f"<tr><td>{e['id']}</td><td>{'cache' if e.get('cached') else (e.get('model') or 'cmd').split('/')[-1]}</td>"
                    f"<td>{e.get('seconds', 0)}s</td><td>{e.get('total', 0)}</td><td>${e.get('cost', 0):.4f}</td></tr>"
                    for e in rows)
                total = sum(e.get("cost", 0) for e in rows)
                out["ledger"] = (f"<h1>Ledger &middot; ${total:.4f}</h1><table><tr><th>step</th><th>model</th>"
                                 f"<th>time</th><th>tokens</th><th>cost</th></tr>{cells}</table>")
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/status"):
            sid = re.search(r"session=(\w+)", self.path)
            self._send(json.dumps(status(sid.group(1) if sid else "")).encode())
        else:
            self._send(PAGE.format(**CFG["page"]).encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/run":
            self._send(b'{"error":"not found"}')
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send(json.dumps({"session": start_run(body["content"])}).encode())


def main() -> int:
    ap = argparse.ArgumentParser(description="HTML UI for a deterministic workflow")
    ap.add_argument("steps_file", type=Path)
    ap.add_argument("--input-file", required=True)
    ap.add_argument("--output", help="step id whose artifact is the result (default: last step)")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    spec = yaml.safe_load(args.steps_file.read_text())
    steps = spec.get("steps") or []
    output_step = args.output or steps[-1]["id"]
    if output_step not in {s["id"] for s in steps}:
        raise SystemExit(f"unknown --output step '{output_step}'")
    CFG.update(root=args.steps_file.parent.resolve(), yaml_name=args.steps_file.name,
               yaml_text=args.steps_file.read_text(), input_file=args.input_file,
               output_step=output_step,
               page={"title": spec.get("workflow", args.steps_file.stem), "nsteps": len(steps),
                     "outstep": output_step, "inputfile": args.input_file})
    print(f"serving {spec.get('workflow')} on http://127.0.0.1:{args.port} · output step: {output_step}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
