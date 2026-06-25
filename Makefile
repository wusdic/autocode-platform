.PHONY: help install dev-install test lint-sh check-docs ci run

help:
	@echo "make install      安装运行时依赖"
	@echo "make dev-install  安装开发+测试依赖"
	@echo "make test         运行单元测试"
	@echo "make lint-sh      bash 语法检查所有 .sh"
	@echo "make check-docs   校验手册嵌入代码块与语法"
	@echo "make ci           本地跑全套 CI（test + lint-sh + check-docs）"
	@echo "make run          启动控制平面（默认 127.0.0.1:9000；局域网设 PLATFORM_BIND_HOST=0.0.0.0）"

install:
	python3 -m pip install -r requirements.txt

dev-install:
	python3 -m pip install -r requirements-dev.txt

test:
	python3 -m pytest -q

lint-sh:
	@for f in platform/*.sh scripts/*.sh; do echo "bash -n $$f"; bash -n "$$f"; done

check-docs:
	python3 scripts/check_docs.py

ci: test lint-sh check-docs

run:
	PLATFORM_BIND_HOST="$${PLATFORM_BIND_HOST:-127.0.0.1}" \
	PLATFORM_TOKEN="$${PLATFORM_TOKEN:-change-me}" \
	python3 -m uvicorn control_plane:app --app-dir platform \
	  --host "$${PLATFORM_BIND_HOST:-127.0.0.1}" --port 9000
