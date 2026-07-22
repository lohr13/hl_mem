@echo off
:: hl_mem production startup script
set HL_MEM_ENV=production
set HL_MEM_EMBEDDER=real
set HL_MEM_RERANKER=real
cd /d D:\workspace\hl_agent\hl_mem
.venv\Scripts\python.exe -m uvicorn hl_mem.api.server:app --host 127.0.0.1 --port 8200
