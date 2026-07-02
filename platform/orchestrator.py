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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import fcntl  # POSIX 文件锁；非 POSIX 平台退化为无锁
except ImportError:  # pragma: no cover
    fcntl = None

import qa_integrity
from control_plane import HermesGateway, Project, Settings

ARCH_GOAL = "产出 ADR + interface-spec + code-spec + TODO：基于 design/PRD.md"
ARCH_WORKERS = ["arch-simple", "arch-scale", "arch-security"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit_append(ws, actor: str, action: str, detail: Optional[dict] = None,
                 result: str = "ok") -> None:
    """统一审计事件流：append 一条到 ``<ws>/.autocode/audit.jsonl``。

    记录"何时(ts)/谁(actor)/做了什么(action)/细节(detail)/结果(result)"，让"什么时候
    什么地方发生了什么"可一站式回溯。控制平面与编排器共用同一格式（各自写，避免耦合导入）。
    失败即静默（审计不该影响主流程）。
    """
    try:
        d = Path(ws) / ".autocode"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "audit.jsonl"
        try:  # 轮转（与 control_plane._rotate_if_big 同口径 5MB 单代）：防长期运行无界膨胀
            if f.exists() and f.stat().st_size > 5_000_000:
                f.replace(f.with_name(f.name + ".1"))
        except OSError:
            pass
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now(), "actor": actor, "action": action,
                                 "detail": detail or {}, "result": result},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


def _done(card: dict) -> bool:
    """卡是否完成（容忍不同字段/取值）。"""
    status = str(card.get("status", "")).lower()
    return status in {"done", "completed", "complete"} or card.get("last_event") == "done"


@contextmanager
def tick_lock(data_root: str):
    """跨进程互斥锁（`{data_root}/.orchestrator.lock`）。

    保证 systemd timer 拉起的 orchestrator 与控制平面内嵌 loop **不并发 tick**，也防"慢 tick
    与下一次 timer tick 重叠"导致同一项目状态 read-modify-write 竞争。拿不到锁就 yield False
    （本轮跳过，下一轮再来；tick 本身幂等，跳过无害）。
    """
    p = Path(data_root)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lockf = open(p / ".orchestrator.lock", "w")
    got = True
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                got = False
        yield got
    finally:
        if got and fcntl is not None:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN)
            except OSError:
                pass
        lockf.close()


def provider_paused(data_root: str) -> bool:
    # 临时过载（.provider_pause，含 until-epoch）或余额耗尽（.provider_billing_dead，永久直至充值）
    # 都不起新 swarm/卡。余额耗尽是 monitor 检测 1113 写的，续跑/重试无意义（D13/D19）。
    if (Path(data_root) / ".provider_billing_dead").exists():
        return True
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
        # canonical 文件，**不放宽**到 *approved*（否则 not-approved-yet.md 之类会误开 dev 闸）。
        f = ws / "design" / "approved_versions.txt"
        return f.exists() and bool([l for l in f.read_text().splitlines() if l.strip()])

    @staticmethod
    def _warn(ws: Path, payload: dict) -> None:
        """非阻断告警落 .autocode/warnings.jsonl（monitor/Web UI 可展示）。"""
        try:
            d = ws / ".autocode"
            d.mkdir(parents=True, exist_ok=True)
            with (d / "warnings.jsonl").open("a", encoding="utf-8") as fh:
                payload = dict(payload); payload["ts"] = _now()
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @classmethod
    def _design_doc(cls, ws: Path, canonical: str, kind: str) -> bool:
        """检测设计产出文件。优先 canonical（design/PRD.md / design/ADR.md）；canonical 缺失时
        容忍 agent 自由命名（design/*<kind>*.md），但落 noncanonical 警告——长期仍靠模板约束标准名。
        """
        if (ws / "design" / canonical).exists():
            return True
        design = ws / "design"
        if design.exists():
            for f in sorted(design.glob("*.md")):
                if kind in f.name.lower():
                    cls._warn(ws, {"type": "noncanonical_design_filename",
                                   "kind": kind, "file": f"design/{f.name}"})
                    return True
        return False

    @staticmethod
    def _release_manifest_ok(ws: Path) -> bool:
        """release 必须产出 reports/release/manifest.json，complete 以它为准（而非仅 release 卡 done）。"""
        f = ws / "reports" / "release" / "manifest.json"
        if not f.exists():
            return False
        try:
            return isinstance(json.loads(f.read_text()), dict)
        except (ValueError, OSError):
            return False

    @staticmethod
    def _qa_cards_done(cards: list) -> bool:
        qa = [c for c in cards if str(c.get("assignee", "")) == "qa"]
        return bool(qa) and all(_done(c) for c in qa)

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
    def _dev_complete(cards: list, ws: Path) -> bool:
        """dev 是否完成。正常路径：dev-worker fan-out 卡全 done。direct-to-QA 路径（D30，
        真机 demo1：dev-lead 串行直接交付、无 fan-out 卡）：无 dev-worker 卡但 dev-lead 卡已 done
        且**已有真实源码落地 + git 有真实提交**。

        为什么加提交判据（第十轮 P0）：dev-lead 是 no-code 角色（无 terminal），不可能自己
        commit/test；若只看"文件存在"就进 QA，未提交的散码会在 release 前被 min_release_ok
        的提交硬闸拦下，而修复卡又派给不能 commit 的 dev-lead → 死锁。提交判据与
        min_release_ok 同口径（commit_count_all>1），把"进 QA"与"能 release"对齐；
        未提交的场景由 tick 的 baseline-validation 自愈建卡交给**能** commit 的 dev-worker。"""
        dev = [c for c in cards if str(c.get("assignee", "")).startswith("dev-worker")]
        if dev:
            return all(_done(c) for c in dev)
        lead_done = any(_done(c) and str(c.get("assignee", "")) == "dev-lead" for c in cards)
        return (lead_done and qa_integrity.expected_files_present(ws)
                and qa_integrity.commit_count_all(ws) > 1)

    @staticmethod
    def _release_done(cards: list) -> bool:
        rel = [c for c in cards if str(c.get("assignee", "")) == "release"]
        return bool(rel) and all(_done(c) for c in rel)

    # --- 状态机 -----------------------------------------------------------
    def tick(self, project: Project) -> str:
        ws = Path(project.workspace)
        state = self._load_state(ws)
        prev_stage = state.get("stage", "created")   # 审计：记录阶段跃迁
        cards = self._cards(project)
        paused = provider_paused(self.data_root)
        changed = False

        adr_present = self._design_doc(ws, "ADR.md", "adr")

        # 1) PRD 产出 → 起架构委员会 swarm（限流暂停期不起）
        if self._design_doc(ws, "PRD.md", "prd") and not state.get("arch_started") and not paused:
            self.gw.swarm(project, ARCH_GOAL, ARCH_WORKERS, "arch-critic", "arch-synthesizer")
            state.update(arch_started=True, stage="architecture"); changed = True

        # 2) ADR + 开闸钥匙 → 让 dev-lead 切编码任务（限流暂停期不起新工作）
        if adr_present and self._approved(ws) \
                and not state.get("dev_started") and not paused:
            self.gw.kanban_create(project, "切分编码任务并链接依赖：基于 design/ADR.md + design/TODO.md",
                                  "dev-lead", "--goal")
            state.update(dev_started=True, stage="development"); changed = True

        # 2b) 自愈：ADR 已出但缺 canonical approved_versions.txt → 建补齐卡（不放宽闸门）。
        #     真机 shi：arch-synthesizer 未写该文件 → 流水线卡死在 architecture，需人工补。
        if adr_present and not self._approved(ws) and not state.get("dev_started") \
                and not state.get("approval_repair_started") and not paused:
            self.gw.kanban_create(
                project,
                "补齐架构批准文件：核对 ADR/TODO 与 critic 意见，blocking issues 已修则写 "
                "design/approved_versions.txt（每行一个已批准版本号）",
                "arch-synthesizer", "--goal")
            state.update(approval_repair_started=True); changed = True

        # 2c) 自愈（第十轮 P0）：direct-to-QA 场景下源码落地但**无真实提交**——dev-lead 无 terminal
        #     不能 commit/test，若不介入会卡死在 dev→qa（_dev_complete 的提交判据不满足）。
        #     建一张 baseline-validation 卡交给**能** commit 的 dev-worker（幂等）。
        if state.get("dev_started") and not state.get("qa_started") \
                and not state.get("baseline_validation_started") and not paused:
            dev_cards = [c for c in cards if str(c.get("assignee", "")).startswith("dev-worker")]
            lead_done = any(_done(c) and str(c.get("assignee", "")) == "dev-lead" for c in cards)
            if (not dev_cards and lead_done
                    and qa_integrity.expected_files_present(ws)
                    and qa_integrity.commit_count_all(ws) <= 1):
                self.gw.kanban_create(
                    project,
                    "baseline-validation：核对已有实现是否匹配 ADR/TODO，跑测试，git commit 为基线",
                    "dev-worker-1", "--goal")
                state.update(baseline_validation_started=True); changed = True
                audit_append(ws, "orchestrator", "baseline_validation",
                             {"reason": "源码落地但无真实提交；dev-lead 无 terminal，建 dev-worker 基线卡"})

        # 3) dev 完成（fan-out 卡全 done，或 direct-to-QA 路径）→ 起 QA（限流暂停期不起）
        if state.get("dev_started") and self._dev_complete(cards, ws) \
                and not state.get("qa_started") and not paused:
            self.gw.kanban_create(project, "全量 QA：基于 acceptance_core，并写 reports/qa/status.json",
                                  "qa", "--goal")
            state.update(qa_started=True, stage="qa"); changed = True

        # 3b) 自愈：QA 卡已 done 但 reports/qa/status.json 缺失 → 建 QA 补齐卡（assignee=qa）。
        #     真机 shi：QA 未写 status.json → 流水线卡在 QA→release，需人工补。
        if state.get("qa_started") and self._qa_cards_done(cards) and not self._qa_status(ws) \
                and not state.get("release_started") and not state.get("qa_repair_started") and not paused:
            self.gw.kanban_create(
                project,
                "补齐 QA 结论：把测试结果写入 reports/qa/status.json（必含 release_allowed 字段）",
                "qa", "--goal")
            state.update(qa_repair_started=True); changed = True

        # 4) 本轮 QA 已起 + QA 放行 → 起 release。要求 qa_started，避免残留旧
        #    reports/qa/status.json（release_allowed=true）在本轮未跑 QA 时误触发 release。
        qa_status = self._qa_status(ws)   # 一次读盘，供放行判断与完整性硬闸共用
        if state.get("qa_started") and qa_status.get("release_allowed") is True \
                and not state.get("release_started") and not paused:
            # 独立交付完整性硬闸（不信任 agent 汇报）：dev 卡 done 却无任何提交/源码落地，
            # 或 status.json 的 integrity 块未通过 → 不起 release，建一张人工 review 卡（幂等）。
            ok, reason = qa_integrity.min_release_ok(
                ws, self._dev_complete(cards, ws), qa_status)
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

        # 5) release 完成 → 项目完成。要求 release 卡 done **且** 产出 reports/release/manifest.json
        #    （"done = 真的交付了"，而非仅看板卡 done）。manifest 缺失则建补齐卡（幂等）。
        if state.get("release_started") and self._release_done(cards) and state.get("stage") != "complete":
            if self._release_manifest_ok(ws):
                state.update(stage="complete", completion_mode="natural"); changed = True
                # D31（第十轮重设计）：complete 后归档所有仍非 done 的遗留卡——被 repair
                # 旁路 supersede 的 blocked 卡会永久停留、污染看板统计与 Web UI 进度。
                # 在 complete 时点归档是**确定性**判据（项目已交付，非 done 卡定属噪音），
                # 比"猜哪张卡被谁 supersede"可靠。cancel_card 失败仅记审计（不阻断收口）。
                cancel = getattr(self.gw, "cancel_card", None)
                if cancel is not None:
                    for c in cards:
                        if not _done(c) and c.get("id"):
                            ok = False
                            try:
                                ok = bool(cancel(project, str(c["id"])))
                            except Exception:
                                ok = False
                            audit_append(ws, "orchestrator", "card_archived",
                                         {"task_id": c.get("id"),
                                          "title": str(c.get("title", ""))[:80],
                                          "status": c.get("status")},
                                         "ok" if ok else "error")
            elif not state.get("manifest_repair_started"):
                self.gw.kanban_create(
                    project,
                    "补齐发布清单：写 reports/release/manifest.json（version/merged_branches/"
                    "artifacts/run_command/notes）",
                    "release", "--goal")
                state.update(manifest_repair_started=True); changed = True

        if changed:
            self._save_state(ws, state)
        new_stage = state.get("stage", "created")
        if new_stage != prev_stage:   # 审计：阶段跃迁落 audit.jsonl（谁=编排器）
            audit_append(ws, "orchestrator", "stage_transition",
                         {"from": prev_stage, "to": new_stage})
        return new_stage

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
    # 跨进程锁：与控制平面内嵌 loop / 上一轮慢 tick 互斥，拿不到就跳过本轮（幂等，无害）。
    with tick_lock(settings.data_root) as got:
        if not got:
            print(f"{_now()} 另一个 orchestrator tick 正在运行，跳过本轮")
            return 0
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
