PY := .venv/bin/python

.PHONY: venv seed smoke probe chat chat-gemini server clean

venv:            ## create venv and install package + LLM clients
	python3 -m venv .venv
	$(PY) -m pip install -q -U pip
	$(PY) -m pip install -q -e ".[llm]"

seed:            ## build the SQLite sample GL (stdlib only)
	python3 scripts/seed_sample_data.py

smoke:           ## engine + wiki tests against the sample (stdlib only)
	python3 scripts/smoke_test.py

probe:           ## end-to-end MCP server check without an LLM
	$(PY) scripts/mcp_probe.py

chat:            ## chat REPL (provider from config.yaml, default ollama)
	$(PY) -m pstb.client.chat

chat-gemini:     ## chat REPL with Gemini on Vertex AI
	$(PY) -m pstb.client.chat --provider gemini

server:          ## run the MCP server standalone (for MCP Inspector etc.)
	$(PY) -m pstb.server

clean:
	rm -rf .venv sample_data/ps_sample.db
