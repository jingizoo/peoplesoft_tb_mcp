setup:
	python3 scripts/setup.py

PY := .venv/bin/python

.PHONY: venv seed unittest smoke probe qa chat chat-gemini server clean

venv:            ## create venv and install package + LLM clients + web UI
	python3 -m venv .venv
	$(PY) -m pip install -q -U pip
	$(PY) -m pip install -q -e ".[llm,gui]"

seed:            ## build the SQLite sample GL (stdlib only)
	python3 scripts/seed_sample_data.py

smoke:           ## engine + wiki tests against the sample (stdlib only)
	python3 scripts/smoke_test.py

unittest:        ## focused scope, session, and evidence-gate regressions
	$(PY) -m unittest discover -s tests -v

probe:           ## end-to-end MCP server check without an LLM
	$(PY) scripts/mcp_probe.py

qa: seed unittest smoke probe  ## full local quality gate

chat:            ## chat REPL (provider from config.yaml, default ollama)
	$(PY) -m pstb.client.chat

chat-gemini:     ## chat REPL with Gemini on Vertex AI
	$(PY) -m pstb.client.chat --provider gemini

server:          ## run the MCP server standalone (for MCP Inspector etc.)
	$(PY) -m pstb.server

clean:
	rm -rf .venv sample_data/ps_sample.db
