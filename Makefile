PORT ?= 8888
PIDFILE := /tmp/orch-server.pid
ENV_FILE := .env

# Load .env if it exists (makes exports available to sub-shells invoked by make)
ifneq (,$(wildcard $(ENV_FILE)))
  include $(ENV_FILE)
  export
endif

.PHONY: start stop restart status install

install:
	@echo "Installing Python dependencies..."
	~/.local/bin/pip install -r requirements.txt -q
	@echo "Done. Copy .env.example → .env and fill in GITHUB_TOKEN."

start:
	@echo "Starting server..."
	@python3 -m web.server & echo $$! > $(PIDFILE)
	@sleep 1
	@echo "Server started (PID $$(cat $(PIDFILE))) at http://localhost:$(PORT)"

stop:
	@if [ -f $(PIDFILE) ]; then \
		PID=$$(cat $(PIDFILE)); \
		echo "Stopping server (PID $$PID)..."; \
		kill $$PID 2>/dev/null && echo "Server stopped." || echo "Process not running."; \
		rm -f $(PIDFILE); \
	else \
		echo "No server PID file found — nothing to stop."; \
	fi

restart:
	@$(MAKE) --no-print-directory stop
	@sleep 1
	@$(MAKE) --no-print-directory start

status:
	@if [ -f $(PIDFILE) ] && kill -0 $$(cat $(PIDFILE)) 2>/dev/null; then \
		echo "Server is running (PID $$(cat $(PIDFILE)))"; \
	else \
		echo "Server is not running."; \
	fi
