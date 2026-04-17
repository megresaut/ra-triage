.PHONY: install dev build lint typecheck

install:
	npm install

dev:
	npm run dev -- --port 1313 --host 127.0.0.1

build:
	npm run build

typecheck:
	npm run typecheck

lint:
	npm run lint && npm run typecheck
