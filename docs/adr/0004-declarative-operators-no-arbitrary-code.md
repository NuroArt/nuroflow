# ADR-0004: Declarative operators, no arbitrary user code

- **Status:** Accepted
- **Date:** 2026-11-04

## Context

Airflow DAGs are Python files, so anyone who can author a DAG can execute code on the workers.
NuroFlow is multi-user and API-driven. Accepting user Python over HTTP would be remote code
execution by design.

## Decision

- A workflow is **data**: YAML/JSON validated by a Pydantic `WorkflowSpec`.
- Behaviour comes from **operators**: vetted Python classes registered by name (`http.request`,
  `ai.complete`, `ai.structured`, `telegram.send`, `data.store`, `transform.template`,
  `control.delay`, `noop`).
- Each operator declares a Pydantic input model, so definitions are validated **at save time**,
  not at run time.
- New operators are added by platform developers, either in code or through a Python entry-point
  group `nuroflow.operators` (plugin packages).
- String building and data shaping use Jinja2's `SandboxedEnvironment`.

## Alternatives

| Option | Why rejected (for now) |
|---|---|
| User Python executed in a container sandbox (gVisor/Firecracker) | Large operational surface; a good future "code operator" |
| WebAssembly sandbox | Promising, but immature tooling for Python |

## Consequences

- ✅ Workflows can be validated, diffed, versioned and visualized as plain data.
- ✅ Hard security boundary.
- ⚠️ Less flexible than Python DAGs. That's mitigated by `http.request` (call anything) and plugins.
