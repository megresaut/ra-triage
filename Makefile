.PHONY: install dev backend frontend test lint db-init clean

install:
	$(MAKE) -C backend install && $(MAKE) -C frontend install

db-init:
	$(MAKE) -C backend db-init

backend:
	$(MAKE) -C backend dev

frontend:
	$(MAKE) -C frontend dev

dev:
	./dev.sh

test:
	$(MAKE) -C backend test

lint:
	$(MAKE) -C backend lint && $(MAKE) -C frontend lint

clean:
	bash backend/scripts/reset_triage_state.sh
