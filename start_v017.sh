#!/bin/bash
export HL_MEM_RERANKER=on
export HL_MEM_RELEVANCE_GATE_MODE=observe
export HL_MEM_QUERY_EXPANSION_MODE=auto
export HL_MEM_QUERY_CONTEXT_MODE=coreference
cd /d/workspace/hl_agent/hl_mem
exec .venv/Scripts/python.exe start_server.py
