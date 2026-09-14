<div align="center">

# Archbro

### Humans guide. Agents reason. One shared project truth.

**Archbro is a human-governed Strands agent that keeps software architecture, execution work, and implementation evidence aligned as a project changes. It completes the repetitive project-reasoning loop for engineering teams and pauses only when a consequential architecture decision needs human approval.**

[Live Demo](https://archbro-dev2.magicdala.com/) · [Demo Flow](#demo-flow) · [Quick Start](#quick-start) · [Technical Reference](#technical-reference)

Built for the [Agents for Humans Hackathon](https://agentsforhumans.devpost.com/) — **Professional Agents**

[![CI](https://github.com/Magic-Dala/archbro-aws/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Magic-Dala/archbro-aws/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

</div>

> **Competition scope:** Archbro's primary competition agent is the built-in agent implemented with the **Strands Agents SDK**. Browser-native WebMCP is an optional interoperability layer that lets external agents enter the same governed workspace; it does not replace the built-in Strands agent.

## The problem

AI can produce code quickly, but software teams repeatedly lose time rebuilding the context around that code:

- What is the project trying to achieve?
- Which architecture was actually approved?
- Which tasks are active, blocked, or already complete?
- Does the implementation still match the design?
- Is a suggested change routine execution, or a consequential architecture decision?

Those questions are judgment-heavy and recur every time a new developer or coding agent joins the work. The usual result is a collection of separate plans, stale diagrams, disconnected task lists, and architecture changes that are difficult to review after the fact.

Archbro gives humans and agents one governed project workspace instead of another isolated chat or plan.

## Who Archbro is for

Archbro is designed for software engineering teams, technical founders, makers, and small product teams that already use AI agents but still need a reliable way to preserve project intent and human control.

It is especially useful when multiple people or agents work on the same codebase and repeatedly need to understand the same architecture, evidence, tasks, and prior decisions.

## What Archbro takes off the team's plate

| Repetitive professional work | What Archbro does |
| --- | --- |
| Reconstruct project context | Loads the accepted goal, Living Architecture, tasks, observations, and decision history |
| Check whether an answer is grounded | Reads project-scoped evidence only when the request actually needs it |
| Turn discussion into execution | Creates and advances normal tasks through deterministic backend boundaries |
| Compare design with implementation | Keeps human-approved Living Architecture separate from revision-pinned Code Architecture evidence |
| Detect architecture drift | Produces a reviewable recommendation instead of silently rewriting project truth |
| Preserve continuity | Makes accepted decisions and durable evidence available to the next request |

## How Archbro works

```text
User request
    ↓
Load accepted project context
    ↓
Built-in Strands agent reasons about the request
    ↓
Read project-scoped evidence only when needed
    ↓
Validate the result against governance rules
    ├── Grounded answer
    ├── Allowed task or observation update
    └── Pending architecture proposal
             ↓
       Human accept / reject
             ↓
Accepted change becomes context for the next request
```

The important distinction is not simply that Archbro can call tools. It understands three different outcomes:

1. **Answer:** explain the current project using its accepted context and available evidence.
2. **Work:** perform an allowed, routine project operation such as creating or advancing a task.
3. **Proposal:** pause at the governance boundary when the requested result would materially change the accepted architecture.

Routine work can continue. Consequential changes cannot become project truth without a human.

## End-to-end example

A user asks:

> How should we handle user authentication?

Archbro does not answer from a blank prompt and does not let a model rewrite the architecture directly.

1. It loads the project's accepted goal, architecture, tasks, observations, and prior decisions.
2. The built-in Strands agent determines whether the question can be answered from current project context or requires repository evidence.
3. When evidence is necessary, Archbro exposes only approved, project-scoped, read-only tools.
4. The agent explains the recommendation and its evidence.
5. If the recommendation fits inside the accepted architecture, Archbro returns a grounded answer or creates normal work.
6. If it requires a structural change, Archbro creates a `PENDING` proposal while the current architecture remains unchanged.
7. A human accepts or rejects the proposal in the Archbro workspace.
8. An accepted update becomes the new shared context for future people and agents.

## Human governance is a product boundary

| The agent can | Only a human can |
| --- | --- |
| Read accepted project context | Accept an architecture proposal |
| Use approved evidence tools | Reject an architecture proposal |
| Return answers with sources | Promote a proposal into shared project truth |
| Create and advance routine tasks | Approve the next accepted architecture version |
| Record project observations | Decide consequential design trade-offs |
| Submit architecture recommendations |  |

There is deliberately no agent-accessible architecture Accept or Reject tool. Backend governance validates every decision before state changes, and only explicit human acceptance increments the accepted architecture version.

## One project truth, two architecture views

Archbro separates design authority from implementation evidence:

- **Living Architecture** is the human-approved canonical design intent. It uses stable component identities and changes only through the review boundary.
- **Code Architecture** is revision-pinned implementation evidence derived from one exact repository commit. It can reveal implementation drift, but publishing it never mutates Living Architecture.

This separation prevents a repository snapshot, model inference, or external agent from quietly becoming the new design authority.

## Why this is a Professional Agent

The [Professional Agents](https://agentsforhumans.devpost.com/) track asks for agents that make people dramatically better at work by handling repetitive, judgment-heavy tasks. Archbro addresses that directly:

| Hackathon goal | Archbro evidence |
| --- | --- |
| Solve a real professional problem | Engineering teams repeatedly rebuild project context and reconcile architecture with implementation |
| Handle work end to end | Context loading, reasoning, evidence retrieval, validation, delivery, and review all happen in one product loop |
| Use Strands non-trivially | The built-in reasoning runtime, Gemini model adapter, and dynamically gated evidence tools are implemented with Strands |
| Surface only real decisions | Routine answers and work continue; consequential architecture changes become human-reviewable proposals |
| Deliver a coherent product | The live workspace combines project goals, architecture, tasks, evidence, agent conversations, and review |
| Demonstrate a working system | A public live deployment and automated CI are included |

## Strands Agents SDK implementation

Strands is the runtime behind Archbro's built-in agent, not a presentation-only dependency.

| Strands capability | How Archbro uses it | Implementation |
| --- | --- | --- |
| `strands.Agent` | Runs the built-in reasoning and tool-use loop | `src/archbro/backend/llm/gemini.py` |
| `strands.models.gemini.GeminiModel` | Connects the agent to an invocation-scoped Gemini client | `src/archbro/backend/llm/gemini.py` |
| `strands.types.tools.AgentTool` | Powers Archbro's JSON-schema-native `SchemaMcpTool` adapter for approved MCP evidence tools | `src/archbro/backend/mcp/strands_adapter.py` |
| Dynamic tool selection | Exposes only the smallest approved tool set needed for the current message | `src/archbro/backend/mcp/agent_tools.py` |
| Structured decision output | Feeds model results into Archbro validation and governance before mutation | `src/archbro/backend/` |

The current default model is Gemini through the Strands Gemini adapter. Production can use Vertex AI with Application Default Credentials, while deterministic tests use the fake provider and require no paid model call.

## Architecture at a glance

| Layer | Responsibility | Technology |
| --- | --- | --- |
| Workspace | Ask, explore architecture, manage tasks, and review proposals | Browser UI + native WebMCP surface |
| Identity | Authenticate users and issue verified identity tokens | Firebase Authentication |
| Secure API | Enforce identity, project access, and API contracts | FastAPI |
| Project state | Store goals, accepted architecture, tasks, evidence, and history | PostgreSQL |
| Governance | Validate answers, work, recommendations, and human decisions | Archbro backend contracts |
| Built-in agent | Understand the request, select evidence, reason, and explain | Strands Agents SDK + Gemini |
| Evidence | Read project-scoped implementation and connected-source facts | GitHub MCP and optional connected providers |
| Deployment | Build, test, deploy, and expose the live product securely | Docker, GitHub Actions, GCE, Cloudflare Tunnel |

The primary product path is:

```text
Human
  → Archbro Workspace
  → Secure FastAPI backend
  → Built-in Strands agent
  → Approved project evidence
  → Grounded answer or pending proposal
  → Human review when required
```

An optional external agent may enter through Archbro's semantic WebMCP Site Tools, but it receives the same project-scoped context and remains subject to the same governance boundary.

## What makes Archbro different

### One shared project truth

People, the built-in agent, and optional external agents operate on the same accepted goal, architecture, tasks, evidence, and decision history instead of maintaining separate plans.

### Evidence is not authority

GitHub, Code Architecture, observations, and connected providers contribute evidence. They do not automatically overwrite the human-approved Living Architecture.

### Routine work and consequential change are different operations

Creating a task, recording an observation, and explaining current state are not treated like accepting a new architecture. Each operation has its own deterministic boundary.

### The agent is embedded in the project workflow

Archbro does not stop at producing text. Its reasoning is connected to durable project state, task execution, evidence, and a human decision loop.

### External agents do not bypass governance

WebMCP gives external agents structured Site Tools instead of DOM guessing, but it does not give them an unsafe direct architecture-mutation path.

## Demo flow

Open the public deployment:

**https://archbro-dev2.magicdala.com/**

A concise end-to-end demonstration can follow this path:

1. Open an existing project with an accepted Living Architecture and active tasks.
2. Ask the built-in agent to explain one architecture area.
3. Ask a repository-specific question so the agent must use approved GitHub evidence.
4. Create or advance one normal task.
5. Ask for a structural architecture change.
6. Show that the recommendation becomes `PENDING` while the accepted architecture remains unchanged.
7. Accept or reject the proposal as the human reviewer.
8. Show that an accepted decision becomes part of the next request's context.

The repository also contains a focused recording script in [`docs/DEMO.md`](docs/DEMO.md).

## Optional WebMCP interoperability

Archbro exposes semantic browser-native Site Tools through:

```js
document.modelContext.registerTool(...)
```

This lets a compatible external host agent read current project reality, perform allowed work, and submit reviewable recommendations without relying on DOM automation.

<details>
<summary><strong>Default Archbro WebMCP Site Tools</strong></summary>

When no connected MCP gateway is configured, Archbro exposes 14 semantic Site Tools:

| Tool | Purpose |
| --- | --- |
| `archbro_ping` | Verify the native WebMCP connection without mutation or model invocation |
| `archbro_get_agent_context` | Read compact project and connected-source context |
| `archbro_get_architecture_diagram` | Read root or subsystem projections from the backend-authored Living Architecture graph |
| `archbro_get_architecture_node_context` | Read bounded upstream and downstream context for a stable Living Architecture node |
| `archbro_find_architecture_path` | Find a directed authored dependency path between architecture nodes |
| `archbro_bootstrap_project` | Commit Architecture v1 only after validated hierarchical planning |
| `archbro_expand_architecture_scope` | Submit an additive one-level decomposition proposal under an existing component |
| `archbro_get_architecture_decision_context` | Read accepted architecture, execution state, evidence, and governance rules |
| `archbro_submit_architecture_recommendation` | Submit an architecture recommendation as `PENDING` human review |
| `archbro_publish_code_architecture` | Persist revision-pinned Code Architecture evidence without changing Living Architecture |
| `archbro_get_code_architecture` | Read the latest persisted Code Architecture evidence snapshot |
| `archbro_create_task` | Create normal execution work inside the accepted Living Architecture |
| `archbro_update_task_status` | Start or complete a task through the deterministic task boundary |
| `archbro_record_project_observation` | Persist external evidence or project facts without misrepresenting them as accepted architecture |

When connected MCP servers are configured, Archbro can also expose gateway discovery and call tools. The calling external agent owns its reasoning; Archbro still owns validation, project state, governance, and deterministic execution.

See [`docs/WEBMCP.md`](docs/WEBMCP.md) for the complete contract and invariants.

</details>

### WebMCP acceptance mode

Open:

```text
/?mode=webmcp
```

This mode disables built-in architecture generation, built-in agent messaging, the human New Project flow, and manual task Start or Done controls so an external-agent acceptance run cannot silently fall back to browser automation. Human architecture Accept and Reject remain enabled.

## Security and evidence boundaries

Connected-provider access is principal-scoped and fail-closed.

- Public or tunneled routes require a verified per-user identity rather than a shared development principal.
- GitHub uses the official MCP read-only mode plus an Archbro backstop: only tools explicitly advertising `annotations.readOnlyHint=true` are exposed or callable.
- Missing, false, malformed, or unknown read-only metadata is rejected before provider dispatch.
- Google Drive requests read-only Drive access.
- Microsoft Teams is read-only by default; write scopes appear only when explicitly enabled.
- Privileged project state stays behind FastAPI, Firebase Admin token verification, and project authorization.
- Firebase is used for authentication only. Project data is stored in PostgreSQL, not Firestore.

Tool output is treated as untrusted evidence. It cannot override the Project Goal, accepted Living Architecture, or approval rules.

## Quick start

The recommended local development path needs no Google Cloud credentials or Gemini API key:

```bash
docker compose up -d --wait
```

Requirements:

- Docker Compose v2.24 or newer
- Docker Desktop or another compatible Docker runtime

The command builds the application, starts PostgreSQL, waits for healthy containers, and serves the local product surface.

| Command | What it does |
| --- | --- |
| `docker compose up -d --wait` | Start the application and database, then wait until healthy |
| `docker compose run --rm app python -m pytest` | Run the test suite inside the application image |
| `docker compose logs -f app` | Follow application logs |
| `docker compose down` | Stop the stack and keep database data |
| `docker compose down -v` | Stop the stack and delete the database volume |

The default local configuration uses:

```env
ARCHBRO_ENV=local
ARCHBRO_AUTH_MODE=local
ARCHBRO_PROVIDER=fake
ARCHBRO_PERSISTENCE=postgres
```

The fake provider makes the deterministic development and acceptance paths available without a paid model call.

## Run locally without Docker

```powershell
cd archbro
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m uvicorn archbro.main:app --host 127.0.0.1 --port 8011
```

After Uvicorn starts, open the local product surface. Use `/?mode=webmcp` only for the stricter external-agent acceptance flow.

## Use the real Strands + Gemini path

Create a local `.env`:

```env
ARCHBRO_PROVIDER=gemini
ARCHBRO_ENV=local
ARCHBRO_AUTH_MODE=local
ARCHBRO_PERSISTENCE=postgres

GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-google-cloud-project
GOOGLE_CLOUD_LOCATION=global
GEMINI_MODEL=gemini-3.8-flash

FIREBASE_PROJECT_ID=
ARCHBRO_FIREBASE_API_KEY=
ARCHBRO_FIREBASE_AUTH_DOMAIN=
ARCHBRO_FIREBASE_APP_ID=
```

With Vertex AI enabled, the built-in Strands agent receives an invocation-scoped Google Gen AI client backed by Application Default Credentials. On GCE, grant the runtime service account `roles/aiplatform.user`. For local ADC, run:

```bash
gcloud auth application-default login
```

Do not download a service-account key for the deployed runtime. To use the Gemini Developer API or a compatible gateway instead, set `GOOGLE_GENAI_USE_VERTEXAI=false` and configure `GEMINI_API_KEY` or `GOOGLE_API_KEY`.

See [`.env.example`](.env.example) for the complete configuration surface.

## Reliability and failure semantics

Initial architecture generation uses a bounded, checkpointed planning flow:

```text
SYSTEM_MAP
  → recursive per-scope evaluation
  → RECONCILE
  → deterministic validation
```

Archbro checkpoints every validated phase. Explicit provider rejections such as `408`, `429`, and `5xx` use an Archbro-owned bounded backoff policy. A timeout or connection loss after dispatch remains `UNKNOWN`; Archbro does not silently retry an ambiguous paid request underneath the governance boundary.

A complete reconciliation response that fails deterministic relationship validation receives one bounded repair attempt while the accepted topology phases remain checkpointed. Truncated JSON is never parsed or accepted.

This behavior is covered by the same public configuration fingerprint used by deployed runtimes, without exposing credentials.

## Tests

Run the full deterministic suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run the containerized suite:

```bash
docker compose run --rm app python -m pytest
```

The repository includes contract, regression, governance, browser, and WebMCP acceptance coverage. Important examples include:

- `tests/test_webmcp_golden_flow.py`
- `tests/test_code_architecture.py`
- `qa/probe_webmcp_live.py`
- `qa/test_architecture_canvas.mjs`
- `qa/test_task_architecture_review.mjs`

A real Gemini smoke test runs only when the required credentials are available. Deterministic tests do not require a model call.

## Project layout

```text
frontend/                       Browser workspace + WebMCP registration
src/archbro/backend/            API, core contracts, agent, validation, governance
src/archbro/integrations/       Firebase identity and external integrations
src/archbro/platform/           PostgreSQL, runtime composition, deployment support
tests/                          Contract, regression, and golden-flow coverage
qa/                             Browser and live WebMCP acceptance harnesses
docs/OWNERSHIP.md               Ownership and dependency rules
docs/WEBMCP.md                  WebMCP contract and governance invariants
docs/DEMO.md                    Concise end-to-end demo script
docs/DEVELOPMENT.md             Local development guide
docs/INFRASTRUCTURE.md          Deployment architecture and recovery instructions
```

## Deployment

A production-oriented `Dockerfile` listens on `$PORT` with a default of `8080`. GitHub Actions builds and deploys protected branches as isolated Compose stacks, each with its own PostgreSQL database.

- **Public live application:** https://archbro-dev2.magicdala.com/
- **Main stack:** `/opt/archbro/main`
- **Development stack:** `/opt/archbro/dev`

The application is exposed through Cloudflare Tunnel; the instance does not publish an application HTTP port directly. Environment files are installed separately and are never written into images or deployment workflows.

`/healthz` is the liveness endpoint. It deliberately verifies process availability without touching persistence, preventing a transient database issue from causing a restart storm.

The development `docker-compose.yml` must not be used as a production deployment file. Production uses the dedicated stack configuration under [`deploy/`](deploy/).

## Technical reference

- [WebMCP contract and governance](docs/WEBMCP.md)
- [Development guide](docs/DEVELOPMENT.md)
- [Infrastructure and deployment](docs/INFRASTRUCTURE.md)
- [Ownership and dependency rules](docs/OWNERSHIP.md)
- [Project repository binding](docs/PROJECT_REPOSITORY_BINDING.md)
- [Architecture Canvas design](docs/ARCHITECTURE_CANVAS_V2.md)
- [Demo script](docs/DEMO.md)

## License

Archbro is released under the [MIT License](LICENSE).
