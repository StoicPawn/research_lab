# Research Lab

Standalone shared experiment service for the home mini-PC. It is deliberately **not** part of Tutor LLM or Expert My Rules: those projects can call it through HTTP, while Research Lab keeps its own workspaces, runs and lifecycle.

## What it does

- creates isolated logical workspaces for any project or one-off idea;
- runs bounded Python experiments with a fixed scientific stack (`numpy`, `scipy`, `pandas`, `sympy`, `matplotlib`);
- stores source, stdout/stderr, metadata and generated artifacts per run;
- exposes a small web dashboard and a Bearer-authenticated API;
- lets you delete individual runs or whole workspaces when they are no longer useful;
- keeps all persistent data in its own Docker volume, separate from Tutor LLM and Expert My Rules;
- is designed so future clients/projects only need `RESEARCH_LAB_URL` + `RESEARCH_LAB_TOKEN`.

## Architecture

```text
Tutor LLM -----------\
Expert My Rules ------> Research Lab API :8300 -> workspace store
Any future project ---/                         -> bounded Python child process
ChatGPT/GitHub workflows                        -> artifacts/results
```

No project imports Research Lab internals. Integrations should be thin HTTP clients. Deleting Research Lab data therefore does not delete Tutor workspaces or Expert My Rules ledgers.

## Workspace layout

Persistent data is conceptually:

```text
/data/workspaces/ws-.../
├── workspace.json
└── runs/
    └── run-.../
        ├── main.py
        ├── request.json
        ├── stdout.txt
        ├── stderr.txt
        ├── result.json
        └── <generated artifacts>
```

Workspace deletion is explicit and requires `confirm=true`.

## Install on the mini-PC

```bash
gh repo clone StoicPawn/research_lab ~/projects/research_lab
cd ~/projects/research_lab
cp .env.example .env
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
sed -i "s#LAB_API_TOKEN=.*#LAB_API_TOKEN=$TOKEN#" .env
echo "Research Lab token: $TOKEN"
docker compose --env-file .env up -d --build
curl http://127.0.0.1:8300/health
```

The service binds to localhost by default. Publish it to your private Tailscale network with Tailscale Serve rather than exposing it directly to the public Internet.

## API examples

Create workspace:

```bash
curl -X POST http://127.0.0.1:8300/api/workspaces \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Martingale counterexample search","source_project":"paper-x"}'
```

Run Python:

```bash
curl -X POST http://127.0.0.1:8300/api/workspaces/<workspace-id>/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"sanity check","code":"print(sum(range(10)))"}'
```

Delete a whole workspace:

```bash
curl -X DELETE 'http://127.0.0.1:8300/api/workspaces/<workspace-id>?confirm=true' \
  -H "Authorization: Bearer $TOKEN"
```

## Resource policy for the 8 GB server

Defaults are intentionally conservative:

- one experiment at a time;
- 120 s normal timeout, 300 s hard configurable maximum;
- 2 GB address-space limit per child process;
- one BLAS/OpenMP thread;
- 100 MB maximum file size for generated artifacts.

Tune these in `.env` if needed.

## Security boundary

Experiment code is executed in a dedicated child process with CPU/memory/file/process limits and, in the Docker deployment, the child drops to a dedicated unprivileged UID. Its environment is sanitized and does not receive the Lab API token.

This is a **resource-bounded private experiment runner, not a hardened sandbox for hostile third-party code**. Do not expose the execution API publicly and do not submit untrusted Internet code without reviewing it.

## Scaling later

The workspace/API contract is intentionally independent from the execution implementation. A future version can route runs to another machine or GPU worker while the ACEPC remains the control plane and keeps workspace metadata.
