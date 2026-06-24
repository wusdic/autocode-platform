#!/usr/bin/env python3
"""Orchestrator —— 把"先设计再执行"的全流程做成幂等状态机（阶段 13）。

替代 watchdog 里临时的"PRD→ADR 文件轮询"兜底：按项目读 文件信号 + Kanban 快照，
显式推进 产品→架构→dev→QA→release 各阶段，状态落 workspace/.autocode/state.json。
watchdog 回归只管异常续跑/限流暂停/review 放行。

设计原则：
  * **幂等**：每个阶段有 *_started 标记，重复 tick 不重复建卡/起 swarm。
  * **文件信号优先**（PRD.md/ADR.md/approved_versions/reports/qa/status.json 由角色可靠写出），
    Kanban 状态做补充（"所有 dev 卡 done"才进 QA）。
  * **不依赖真实 Hermes 可测**：注入 gateway（FakeGateway），临时 workspace 跑全阶段。
  * 与 watchdog 一致：供应商限流暂停期间不起新 swarm/卡。

用法：
  orchestrator.py tick --all
  orchestrator.py tick --project demo1
cron（每分钟）：
  * * * * * ~/platform/venv/bin/python ~/platform/orchestrator.py tick --all >> ~/platform/orchestrator.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import qa_integrity
from control_plane import HermesGateway, Project, Settings

ARCH_GOAL = "产出 ADR + interface-spec + code-spec + TODO：基于 design/PRD.md"
ARCH_WORKERS = ["arch-simple", "arch-scale", "arch-security"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _done(card: dict) -> bool:
    """卡是否完成（容忍不同字段/取值）。"""
    status = str(card.get("status", "")).lower()
    return status in {"done", "completed", "complete"} or card.get("last_event") == "done"


def provider_paused(data_root: str) -> bool:
    f = Path(data_root) / ".provider_pause"
    if not f.exists():
        return False
    try:
        return time.time() < int("".join(ch for ch in f.read_text() if ch.isdigit()) or "0")
    except (ValueError, OSError):
        return False


class Orchestrator:
    def __init__(self, gateway: HermesGateway, data_root: str = "/data/projects") -> None:
        self.gw = gateway
        self.data_root = data_root

    # --- IO ---------------------------------------------------------------
    def _state_path(self, ws: Path) -> Path:
        return ws / ".autocode" / "state.json"

    def _load_state(self, ws: Path) -> dict:
        p = self._state_path(ws)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (ValueError, OSError):
                pass
        return {"stage": "created"}

    def _save_state(self, ws: Path, state: dict) -> None:
        p = self._state_path(ws)
        p.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _now()
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def _cards(self, project: Project) -> list:
        try:
            out = self.gw.kanban(project, "list", "--json")
            return out if isinstance(out, list) else []
        except Exception:
            return []

    @staticmethod
    def _approved(ws: Path) -> bool:
        f = ws / "design" / "approved_versions.txt"
        return f.exists() and bool([l for l in f.read_text().splitlines() if l.strip()])

    @staticmethod
    def _qa_status(ws: Path) -> dict:
        f = ws / "reports" / "qa" / "status.json"
        if not f.exists():
            return {}
        try:
            data = json.loads(f.read_text())
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    @classmethod
    def _qa_release_allowed(cls, ws: Path) -> bool:
        return cls._qa_status(ws).get("release_allowed") is True

    @staticmethod
    def _all_dev_done(cards: list) -> bool:
        dev = [c for c in cards if str(c.get("assignee", "")).startswith("dev-worker")]
        return bool(dev) and all(_done(c) for c in dev)

    @staticmethod
    def _release_done(cards: list) -> bool:
        rel = [c for c in cards if str(c.get("assignee", "")) == "release"]
        return bool(rel) and all(_done(c) for c in rel)

    # --- 状态机 -----------------------------------------------------------
    def tick(self, project: Project) -> str:
        ws = Path(project.workspace)
        state = self._load_state(ws)
        cards = self._cards(project)
        paused = provider_paused(self.data_root)
        changed = False

        # 1) PRD 产出 → 起架构委员会 swarm（限流暂停期不起）
        if (ws / "design" / "PRD.md").exists() and not state.get("arch_started") and not paused:
            self.gw.swarm(project, ARCH_GOAL, ARCH_WORKERS, "arch-critic", "arch-synthesizer")
            state.update(arch_started=True, stage="architecture"); changed = True

        # 2) ADR + 开闸钥匙 → 让 dev-lead 切编码任务（限流暂停期不起新工作）
        if (ws / "design" / "ADR.md").exists() and self._approved(ws) \
                and not state.get("dev_started") and not paused:
            self.gw.kanban_create(project, "切分编码任务并链接依赖：基于 design/ADR.md + design/TODO.md",
                                  "dev-lead", "--goal")
            state.update(dev_started=True, stage="development"); changed = True

        # 3) 所有 dev 卡 done → 起 QA（限流暂停期不起）
        if state.get("dev_started") and self._all_dev_done(cards) \
                and not state.get("qa_started") and not paused:
            self.gw.kanban_create(project, "全量 QA：基于 acceptance_core，并写 reports/qa/status.json",
                                  "qa", "--goal")
            state.update(qa_started=True, stage="qa"); changed = True

        # 4) 本轮 QA 已起 + QA 放行 → 起 release。要求 qa_started，避免残留旧
        #    reports/qa/status.json（release_allowed=true）在本轮未跑 QA 时误触发 release。
        qa_status = self._qa_status(ws)   # 一次读盘，供放行判断与完整性硬闸共用
        if state.get("qa_started") and qa_status.get("release_allowed") is True \
                and not state.get("release_started") and not paused:
            # 独立交付完整性硬闸（不信任 agent 汇报）：dev 卡 done 却无任何提交/源码落地，
            # 或 status.json 的 integrity 块未通过 → 不起 release，建一张人工 review 卡（幂等）。
            ok, reason = qa_integrity.min_release_ok(
                ws, self._all_dev_done(cards), qa_status)
            if not ok:
                if not state.get("integrity_blocked"):
                    self.gw.kanban_create(
                        project, f"产物落地校验失败，阻断发布：{reason}", "dev-lead", "--goal")
                    state.update(integrity_blocked=True, integrity_reason=reason)
                    changed = True
            else:
                if state.get("integrity_blocked"):
                    state["integrity_blocked"] = False
                self.gw.kanban_create(project, "发布：QA 通过后合并/打包/部署", "release", "--goal")
                state.update(release_started=True, stage="release"); changed = True

        # 5) release 完成 → 项目完成
        if state.get("release_started") and self._release_done(cards) and state.get("stage") != "complete":
            state.update(stage="complete"); changed = True

        if changed:
            self._save_state(ws, state)
        return state.get("stage", "created")

    def tick_all(self) -> dict:
        root = Path(self.data_root)
        result = {}
        if not root.exists():
            return result
        for d in sorted(root.iterdir()):
            if not (d.is_dir() and (d / ".hermes").exists()):
                continue
            project = Project(project_id=d.name, port=0, key="",
                              home=str(d / ".hermes"), workspace=str(d / "workspace"))
            try:
                result[d.name] = self.tick(project)
            except Exception as exc:  # 单项目失败不影响其它
                print(f"{_now()} [warn] orchestrator {d.name} tick failed: {exc}", file=sys.stderr)
        return result


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="autocode orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    g = t.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true")
    g.add_argument("--project")
    args = ap.parse_args(argv)

    settings = Settings()
    orch = Orchestrator(HermesGateway(settings), data_root=settings.data_root)
    if args.all:
        for pid, stage in orch.tick_all().items():
            print(f"{_now()} project {pid}: stage={stage}")
    else:
        d = Path(settings.data_root) / args.project
        project = Project(project_id=args.project, port=0, key="",
                          home=str(d / ".hermes"), workspace=str(d / "workspace"))
        print(f"{_now()} project {args.project}: stage={orch.tick(project)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
