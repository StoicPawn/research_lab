from __future__ import annotations

import hmac
import json
import os
import resource
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


DATA_DIR = Path(os.getenv("LAB_DATA_DIR", "/data")).resolve()
WORKSPACES_DIR = DATA_DIR / "workspaces"
TOKEN = os.getenv("LAB_API_TOKEN", "").strip()
DEFAULT_TIMEOUT = max(1, int(os.getenv("LAB_DEFAULT_TIMEOUT_S", "120")))
MAX_TIMEOUT = max(DEFAULT_TIMEOUT, int(os.getenv("LAB_MAX_TIMEOUT_S", "300")))
MAX_MEMORY_MB = max(256, int(os.getenv("LAB_MAX_MEMORY_MB", "2048")))
MAX_CODE_BYTES = max(10_000, int(os.getenv("LAB_MAX_CODE_BYTES", "250000")))
MAX_ARTIFACT_BYTES = max(1_000_000, int(os.getenv("LAB_MAX_ARTIFACT_BYTES", "100000000")))
MAX_CONCURRENT_RUNS = max(1, int(os.getenv("LAB_MAX_CONCURRENT_RUNS", "1")))
RUNNER_UID = int(os.getenv("LAB_RUNNER_UID", "10001"))
RUNNER_GID = int(os.getenv("LAB_RUNNER_GID", "10001"))
RUN_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)

WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    source_project: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunCreate(BaseModel):
    title: str = Field(default="Experiment", min_length=1, max_length=200)
    code: str = Field(min_length=1)
    timeout_seconds: int | None = None
    files: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not TOKEN:
        raise HTTPException(status_code=503, detail="LAB_API_TOKEN is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    supplied = authorization[7:]
    if not hmac.compare_digest(supplied, TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")


def _workspace_path(workspace_id: str) -> Path:
    if not workspace_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in workspace_id):
        raise HTTPException(status_code=400, detail="Invalid workspace id")
    path = (WORKSPACES_DIR / workspace_id).resolve()
    if path.parent != WORKSPACES_DIR:
        raise HTTPException(status_code=400, detail="Invalid workspace path")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Corrupt metadata: {path.name}") from exc


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def _safe_relative(base: Path, raw: str) -> Path:
    candidate = (base / raw).resolve()
    if candidate == base or base not in candidate.parents:
        raise HTTPException(status_code=400, detail=f"Unsafe relative path: {raw}")
    return candidate


def _workspace_summary(path: Path) -> dict[str, Any]:
    meta = _read_json(path / "workspace.json")
    runs_dir = path / "runs"
    meta["run_count"] = len([p for p in runs_dir.iterdir() if p.is_dir()]) if runs_dir.exists() else 0
    meta["size_bytes"] = _dir_size(path)
    return meta


def _preexec(timeout_seconds: int, memory_mb: int):
    def apply_limits() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 2))
        memory_bytes = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ARTIFACT_BYTES, MAX_ARTIFACT_BYTES))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(RUNNER_GID)
            os.setuid(RUNNER_UID)
    return apply_limits


def _collect_artifacts(run_dir: Path) -> list[dict[str, Any]]:
    excluded = {"main.py", "request.json", "stdout.txt", "stderr.txt", "result.json"}
    artifacts: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir).as_posix()
        if rel in excluded or rel.startswith(".mplconfig/"):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        artifacts.append({"path": rel, "size_bytes": size})
        if len(artifacts) >= 200:
            break
    return artifacts


def _run_python(run_dir: Path, timeout_seconds: int) -> dict[str, Any]:
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    env = {
        "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/tmp",
        "TMPDIR": str(run_dir),
        "MPLCONFIGDIR": str(run_dir / ".mplconfig"),
        "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }
    started = time.monotonic()
    timed_out = False
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        with RUN_SEMAPHORE:
            proc = subprocess.Popen(
                ["python", "-I", "main.py"],
                cwd=run_dir,
                stdout=out,
                stderr=err,
                env=env,
                preexec_fn=_preexec(timeout_seconds, MAX_MEMORY_MB),
                start_new_session=True,
            )
            try:
                returncode = proc.wait(timeout=timeout_seconds + 5)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                returncode = proc.wait(timeout=5)
    duration = time.monotonic() - started
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")[-100_000:]
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")[-100_000:]
    return {
        "status": "timeout" if timed_out else ("success" if returncode == 0 else "failed"),
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 4),
        "stdout": stdout,
        "stderr": stderr,
    }


app = FastAPI(title="Research Lab", version="0.1.0")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "research-lab",
        "token_configured": bool(TOKEN),
        "max_concurrent_runs": MAX_CONCURRENT_RUNS,
    }


@app.get("/api/capabilities", dependencies=[Depends(require_token)])
def capabilities() -> dict[str, Any]:
    return {
        "python": True,
        "packages": ["numpy", "scipy", "pandas", "sympy", "matplotlib"],
        "default_timeout_seconds": DEFAULT_TIMEOUT,
        "max_timeout_seconds": MAX_TIMEOUT,
        "max_memory_mb": MAX_MEMORY_MB,
        "max_concurrent_runs": MAX_CONCURRENT_RUNS,
        "execution_isolation": "dedicated child process with rlimits and dropped UID inside service container",
        "hostile_code_sandbox": False,
    }


@app.get("/api/workspaces", dependencies=[Depends(require_token)])
def list_workspaces() -> list[dict[str, Any]]:
    items = []
    for path in sorted(WORKSPACES_DIR.iterdir()):
        if path.is_dir() and (path / "workspace.json").exists():
            items.append(_workspace_summary(path))
    return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)


@app.post("/api/workspaces", dependencies=[Depends(require_token)])
def create_workspace(req: WorkspaceCreate) -> dict[str, Any]:
    workspace_id = f"ws-{uuid.uuid4().hex[:12]}"
    path = _workspace_path(workspace_id)
    path.mkdir(parents=True, exist_ok=False)
    (path / "runs").mkdir()
    meta = {
        "id": workspace_id,
        "name": req.name.strip(),
        "description": req.description,
        "tags": req.tags,
        "source_project": req.source_project,
        "metadata": req.metadata,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    _write_json(path / "workspace.json", meta)
    return _workspace_summary(path)


@app.get("/api/workspaces/{workspace_id}", dependencies=[Depends(require_token)])
def get_workspace(workspace_id: str) -> dict[str, Any]:
    path = _workspace_path(workspace_id)
    if not (path / "workspace.json").exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    return _workspace_summary(path)


@app.delete("/api/workspaces/{workspace_id}", dependencies=[Depends(require_token)])
def delete_workspace(workspace_id: str, confirm: bool = Query(False)) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass confirm=true to delete a workspace permanently")
    path = _workspace_path(workspace_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    shutil.rmtree(path)
    return {"ok": True, "deleted_workspace": workspace_id}


@app.get("/api/workspaces/{workspace_id}/runs", dependencies=[Depends(require_token)])
def list_runs(workspace_id: str) -> list[dict[str, Any]]:
    path = _workspace_path(workspace_id)
    runs_dir = path / "runs"
    if not runs_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    runs = []
    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        result = run_dir / "result.json"
        if run_dir.is_dir() and result.exists():
            runs.append(_read_json(result))
    return runs


@app.post("/api/workspaces/{workspace_id}/runs", dependencies=[Depends(require_token)])
def run_experiment(workspace_id: str, req: RunCreate) -> dict[str, Any]:
    workspace = _workspace_path(workspace_id)
    if not (workspace / "workspace.json").exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    code_bytes = len(req.code.encode("utf-8"))
    if code_bytes > MAX_CODE_BYTES:
        raise HTTPException(status_code=413, detail=f"Code exceeds {MAX_CODE_BYTES} bytes")
    timeout_seconds = req.timeout_seconds or DEFAULT_TIMEOUT
    if timeout_seconds < 1 or timeout_seconds > MAX_TIMEOUT:
        raise HTTPException(status_code=400, detail=f"timeout_seconds must be between 1 and {MAX_TIMEOUT}")

    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = workspace / "runs" / run_id
    run_dir.mkdir(parents=True)
    if os.geteuid() == 0:
        os.chown(run_dir, RUNNER_UID, RUNNER_GID)
        os.chmod(run_dir, 0o700)

    for raw, content in req.files.items():
        target = _safe_relative(run_dir, raw)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if os.geteuid() == 0:
            os.chown(target, RUNNER_UID, RUNNER_GID)

    script = run_dir / "main.py"
    script.write_text(req.code, encoding="utf-8")
    request_data = {
        "id": run_id,
        "workspace_id": workspace_id,
        "title": req.title,
        "timeout_seconds": timeout_seconds,
        "metadata": req.metadata,
        "created_at": utc_now(),
    }
    _write_json(run_dir / "request.json", request_data)
    if os.geteuid() == 0:
        os.chown(script, RUNNER_UID, RUNNER_GID)
        for item in run_dir.rglob("*"):
            try:
                os.chown(item, RUNNER_UID, RUNNER_GID)
            except OSError:
                pass

    execution = _run_python(run_dir, timeout_seconds)
    result = request_data | execution
    result["completed_at"] = utc_now()
    result["artifacts"] = _collect_artifacts(run_dir)
    _write_json(run_dir / "result.json", result)

    workspace_meta = _read_json(workspace / "workspace.json")
    workspace_meta["updated_at"] = utc_now()
    _write_json(workspace / "workspace.json", workspace_meta)
    return result


@app.get("/api/workspaces/{workspace_id}/runs/{run_id}", dependencies=[Depends(require_token)])
def get_run(workspace_id: str, run_id: str) -> dict[str, Any]:
    workspace = _workspace_path(workspace_id)
    run_dir = _safe_relative(workspace / "runs", run_id)
    result = run_dir / "result.json"
    if not result.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    return _read_json(result)


@app.delete("/api/workspaces/{workspace_id}/runs/{run_id}", dependencies=[Depends(require_token)])
def delete_run(workspace_id: str, run_id: str, confirm: bool = Query(False)) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(status_code=400, detail="Pass confirm=true to delete a run permanently")
    workspace = _workspace_path(workspace_id)
    run_dir = _safe_relative(workspace / "runs", run_id)
    if not run_dir.exists() or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    shutil.rmtree(run_dir)
    return {"ok": True, "deleted_run": run_id}


@app.get("/api/workspaces/{workspace_id}/runs/{run_id}/artifacts/{artifact_path:path}", dependencies=[Depends(require_token)])
def get_artifact(workspace_id: str, run_id: str, artifact_path: str) -> FileResponse:
    workspace = _workspace_path(workspace_id)
    run_dir = _safe_relative(workspace / "runs", run_id)
    target = _safe_relative(run_dir, artifact_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    if target.stat().st_size > MAX_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="Artifact too large")
    return FileResponse(target)


DASHBOARD = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Research Lab</title>
<style>body{font-family:system-ui;max-width:1050px;margin:auto;padding:20px;background:#fafafa;color:#111}input,textarea,button,select{font:inherit;padding:8px;margin:4px 0}textarea{width:100%;min-height:220px;font-family:ui-monospace,monospace}button{cursor:pointer}.row{display:flex;gap:10px;flex-wrap:wrap}.card{background:white;border:1px solid #ddd;border-radius:10px;padding:14px;margin:12px 0}.muted{color:#666}.bad{color:#a00}.good{color:#073}pre{white-space:pre-wrap;background:#111;color:#eee;padding:12px;border-radius:8px;overflow:auto}</style>
</head><body><h1>Research Lab</h1><p class="muted">Workspace isolati per esperimenti Python riproducibili. I dati restano sul server, separati da Tutor LLM ed Expert My Rules.</p>
<div class="card"><label>API token</label><div class="row"><input id="token" type="password" style="flex:1" placeholder="LAB_API_TOKEN"><button onclick="saveToken()">Salva sessione</button><button onclick="loadAll()">Aggiorna</button></div></div>
<div class="card"><h3>Nuovo workspace</h3><div class="row"><input id="wsname" placeholder="Nome" style="flex:1"><input id="source" placeholder="Progetto sorgente (opzionale)" style="flex:1"><button onclick="createWs()">Crea</button></div><input id="desc" placeholder="Descrizione" style="width:98%"></div>
<div id="workspaces"></div>
<div class="card"><h3>Esegui esperimento</h3><select id="wsselect"></select><input id="title" value="Experiment" style="width:98%"><textarea id="code">import numpy as np\nx=np.arange(10)\nprint('mean=', x.mean())\n</textarea><button onclick="runCode()">Esegui</button><pre id="output">Nessun run.</pre></div>
<script>
const $=id=>document.getElementById(id); $('token').value=sessionStorage.getItem('lab_token')||'';
function saveToken(){sessionStorage.setItem('lab_token',$('token').value);loadAll()}
async function api(path,opts={}){opts.headers={...(opts.headers||{}),'Authorization':'Bearer '+(sessionStorage.getItem('lab_token')||$('token').value),'Content-Type':'application/json'};const r=await fetch(path,opts);let d;try{d=await r.json()}catch{d=await r.text()}if(!r.ok)throw new Error(typeof d==='string'?d:(d.detail||JSON.stringify(d)));return d}
async function loadAll(){try{const ws=await api('/api/workspaces');$('wsselect').innerHTML=ws.map(x=>`<option value="${x.id}">${x.name} (${x.run_count} run)</option>`).join('');$('workspaces').innerHTML=ws.map(x=>`<div class="card"><b>${x.name}</b> <span class="muted">${x.id}</span><br><span>${x.description||''}</span><br><span class="muted">${x.run_count} run · ${(x.size_bytes/1048576).toFixed(2)} MB</span><br><button onclick="showRuns('${x.id}')">Run</button> <button onclick="delWs('${x.id}')">Elimina workspace</button><div id="runs-${x.id}"></div></div>`).join('')||'<div class="card muted">Nessun workspace.</div>'}catch(e){$('workspaces').innerHTML='<div class="card bad">'+e.message+'</div>'}}
async function createWs(){try{await api('/api/workspaces',{method:'POST',body:JSON.stringify({name:$('wsname').value,description:$('desc').value,source_project:$('source').value||null})});$('wsname').value='';$('desc').value='';loadAll()}catch(e){alert(e.message)}}
async function delWs(id){if(!confirm('Eliminare definitivamente workspace e tutti i run?'))return;try{await api('/api/workspaces/'+id+'?confirm=true',{method:'DELETE'});loadAll()}catch(e){alert(e.message)}}
async function showRuns(id){try{const rs=await api('/api/workspaces/'+id+'/runs');document.getElementById('runs-'+id).innerHTML=rs.map(r=>`<pre>${r.id} · ${r.status} · ${r.duration_seconds}s\n${(r.stdout||'').slice(-2000)}\n${(r.stderr||'').slice(-1000)}</pre>`).join('')||'<p class="muted">Nessun run.</p>'}catch(e){alert(e.message)}}
async function runCode(){const id=$('wsselect').value;if(!id)return alert('Crea prima un workspace');$('output').textContent='Esecuzione...';try{const r=await api('/api/workspaces/'+id+'/runs',{method:'POST',body:JSON.stringify({title:$('title').value,code:$('code').value})});$('output').textContent=JSON.stringify(r,null,2);loadAll()}catch(e){$('output').textContent=e.message}}
loadAll();
</script></body></html>'''


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD)


def main() -> None:
    import uvicorn
    host = os.getenv("LAB_HOST", "0.0.0.0")
    port = int(os.getenv("LAB_PORT", "8300"))
    uvicorn.run("research_lab.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
