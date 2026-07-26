# Agent Memory Competitor Comparison

> Updated: 2026-07-26. This is an architectural comparison, not a benchmark result. Product capabilities can change;
> follow the linked official documentation for current deployment and licensing details.

HL-Mem, Mem0, Zep/Graphiti, LangMem, and Letta solve overlapping but different problems. HL-Mem is a local-first memory
service with evidence-backed facts, dual-time queries, lifecycle governance, and a separate experience channel. The
alternatives range from managed memory platforms to libraries embedded directly in an agent runtime.

## Summary

| Project | Primary abstraction | Retrieval and storage | Best fit | Main contrast with HL-Mem |
|---|---|---|---|---|
| Mem0 | Extracted user/session/org memories | Vector search, filters, optional reranking and graph backends | Fast personalization with managed or self-hosted choices | HL-Mem makes evidence, valid/recorded time, TTL/decay, and transactional state transitions first-class |
| Zep / Graphiti | Temporal context graph of episodes, entities, and fact edges | Semantic, keyword, and graph search; Graphiti local or Zep managed | Relationship-heavy, temporally evolving knowledge | HL-Mem keeps SQLite as the complete default backend and uses graph-like relations as bounded expansion rather than the primary store |
| LangMem | Semantic profiles/collections, episodic examples, procedural prompts | Storage-agnostic primitives or LangGraph BaseStore | LangGraph-native memory formation and prompt optimization | HL-Mem is a standalone service with durable jobs, audit/evidence semantics, lifecycle workers, REST/MCP/Hermes adapters |
| Letta | Stateful agent with in-context memory blocks plus archival memory | Always-visible blocks, files, archival vector search, external RAG | Agents that actively manage their own context and memory | HL-Mem decouples memory governance from the agent and returns evidence-aware context through adapters |

## Mem0

Mem0 offers a managed platform and an open-source SDK. Its add flow uses an LLM to extract useful facts, checks existing
memories for duplicates or contradictions, and stores the result in vector storage; its search API supports semantic
retrieval, filters, thresholds, and optional reranking. It scopes memory by identifiers such as user, agent, and run, and
documents conversation, session, user, and organizational layers.

Optional Graph Memory mirrors extracted entities and relationships into Neo4j, Memgraph, Neptune, Kuzu, or Apache AGE.
Vector results remain the primary ordered result set while graph relations are returned alongside them. Mem0 also exposes
per-memory change history and supports managed governance features.

Choose Mem0 when integration speed, broad provider/backend choice, or a managed control plane matters most. Choose HL-Mem
when a single local SQLite deployment, source Evidence links, bitemporal `as_of` behavior, deterministic conflict rules,
importance-aware retention, and cascading forget/stale semantics are central requirements.

Official references: [memory types](https://docs.mem0.ai/core-concepts/memory-types),
[add flow](https://docs.mem0.ai/core-concepts/memory-operations/add),
[search](https://docs.mem0.ai/core-concepts/memory-operations/search), and
[Graph Memory](https://docs.mem0.ai/open-source/features/graph-memory).

## Zep and Graphiti

Graphiti is Zep's open-source temporal knowledge-graph framework. It incrementally builds one Context Graph per subject
from episodes and represents facts as relationships between entity nodes. Facts carry validity timestamps, and the graph
can invalidate outdated facts while preserving history. Retrieval combines semantic, keyword, and graph-aware search.

Zep operates the model as a managed enterprise Context Lake and adds governed multi-graph operation, context assembly,
and proprietary extraction/retrieval components. This makes it a strong fit when temporal relationships and graph
traversal are the core model, or when a managed graph service is desirable.

HL-Mem shares the emphasis on evolving facts but has a different center of gravity: immutable Events and Evidence links,
Claims with valid and recorded time, SQLite transactions, FTS/dense RRF, and explicit lifecycle workers. Its relation
channel is optional and bounded; no graph service is required for the supported deployment.

Official references: [Graphiti overview](https://help.getzep.com/graphiti/getting-started/welcome),
[Zep graph model](https://help.getzep.com/graph-overview), and
[Zep vs Graphiti](https://help.getzep.com/zep-vs-graphiti).

## LangMem

LangMem is a library of memory-management primitives with native LangGraph integration. It distinguishes semantic memory
(schema-bound profiles or searchable collections), episodic memory (past successful interactions), and procedural memory
(behavioral instructions). Memory can be formed in the request path or by a background manager. Its core transformations
are storage-independent; stateful integrations use LangGraph's BaseStore with namespaces, semantic search, and metadata
filters.

LangMem is the natural choice for a LangGraph application that wants customizable extraction/consolidation and prompt
optimization without adopting a separate memory service. The application selects its persistence and governance model.

HL-Mem supplies more infrastructure out of the box: immutable migrations, durable workers, atomic Claim mutation,
evidence lineage, temporal visibility, retention/decay/archive, audited conflict handling, and multiple service adapters.
The trade-off is a more opinionated domain model and operational component.

Official references: [LangMem introduction](https://langchain-ai.github.io/langmem/) and
[core concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/).

## Letta

Letta treats an agent as a persistent stateful service. Memory blocks are structured, agent-editable sections that remain
in the context window; blocks can be read-only or shared across agents. Larger or less important information moves to
files, archival vector memory, or an external RAG system. The agent uses built-in tools to rewrite blocks, search
conversation history, and insert or retrieve archival memories. The Agent Development Environment exposes memory, state,
prompts, and tool execution for inspection.

Letta is strongest when the agent itself should actively curate an always-visible working context and when agent state,
tools, and memory are one runtime abstraction. Its block model is intentionally flexible and agent-centric.

HL-Mem instead treats memory as an independent governed subsystem. Agents submit Events or explicit memories and receive a
budgeted Context Packet; application services and workers own conflict, evidence, expiry, feedback, and forgetting. This
fits multiple agent runtimes that need consistent memory semantics without moving their full execution state into one
agent platform.

Official references: [context hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy),
[memory blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks), and
[ADE](https://docs.letta.com/guides/ade/overview).

## Selection guide

- Pick **HL-Mem** for local-first operation, auditable evidence, bitemporal facts, explicit lifecycle policy, and
  runtime-neutral adapters.
- Pick **Mem0** for a compact add/search integration with flexible managed or self-hosted infrastructure.
- Pick **Zep/Graphiti** when a temporal knowledge graph and relationship traversal are the primary representation.
- Pick **LangMem** when memory should remain a composable LangGraph/library concern.
- Pick **Letta** when persistent agent state and agent-managed in-context memory are the desired programming model.

These choices are not mutually exclusive: LangMem or Letta can consume an external memory service, and HL-Mem can serve
agents through REST, MCP, or Hermes. Evaluate with representative update, contradiction, deletion, temporal, isolation,
latency, and token-budget scenarios rather than relying on feature counts alone.
