.PHONY: help install dev-install test lint-sh run

help:
	@echo "make install      安装运行时依赖"
	@echo "make dev-install  安装开发+测试依赖"
	@echo "make test         运行单元测试"
	@echo "make lint-sh      bash 语法检查所有 .sh"
	@echo "make run          本地启动控制平面（127.0.0.1:9000）"

install:
	python3 -m pip install -r requirements.txt

dev-install:
	python3 -m pip install -r requirements-dev.txt

test:
	python3 -m pytest -q

lint-sh:
	@for f in platform/*.sh scripts/*.sh; do echo "bash -n $$f"; bash -n "$$f"; done

run:
	PLATFORM_TOKEN="$${PLATFORM_TOKEN:-change-me}" \
	python3 -m uvicorn control_plane:app --app-dir platform --host 127.0.0.1 --port 9000
