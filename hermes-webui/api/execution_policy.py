"""Optional, turn-scoped business checks. Hermes keeps planning and dispatch ownership."""
from __future__ import annotations

import json
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


def _names(value, field_name, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError(f"{field_name} must be a list of non-empty strings")
    return frozenset(value)


def _number(config, key, default, maximum):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{key} must be an integer between 0 and {maximum}")
    return value


@dataclass(frozen=True)
class ToolPolicy:
    required_args: frozenset = frozenset()
    argument_types: dict = field(default_factory=dict)
    path_args: frozenset = frozenset()
    required_result_fields: frozenset = frozenset()
    artifact_fields: frozenset = frozenset()
    retry_safe: bool = False

    @classmethod
    def from_config(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Each business tool rule must be a mapping")
        fields = {"required_args", "argument_types", "path_args", "required_result_fields", "artifact_fields", "retry_safe"}
        if set(value) - fields:
            raise ValueError("Unknown business tool rule field")
        types = value.get("argument_types", {})
        valid = {"string", "integer", "number", "boolean", "object", "array"}
        if not isinstance(types, dict) or any(not isinstance(k, str) or v not in valid for k, v in types.items()):
            raise ValueError("Invalid argument_types")
        if not isinstance(value.get("retry_safe", False), bool):
            raise ValueError("retry_safe must be a boolean")
        return cls(**{k: _names(value.get(k, []), k) for k in fields - {"argument_types", "retry_safe"}},
                   argument_types=dict(types), retry_safe=value.get("retry_safe", False))


@dataclass(frozen=True)
class ExecutionPolicy:
    max_tool_calls: int = 100
    max_concurrent_calls: int = 4
    time_budget_seconds: int = 600
    max_retries: int = 5
    max_argument_bytes: int = 2 * 1024 * 1024
    max_artifact_bytes: int = 20 * 1024 * 1024

    @classmethod
    def from_config(cls, value):
        limits = {"max_tool_calls": 100000, "max_concurrent_calls": 128,
                  "time_budget_seconds": 86400, "max_retries": 5,
                  "max_argument_bytes": 20 * 1024 * 1024, "max_artifact_bytes": 200 * 1024 * 1024}
        defaults = cls()
        return cls(**{k: _number(value, k, getattr(defaults, k), maximum) for k, maximum in limits.items()})


def result_object(result):
    if isinstance(result, str):
        if len(result) > 2 * 1024 * 1024:
            return {}
        try:
            result = json.loads(result)
        except (ValueError, TypeError, RecursionError):
            return {}
    if not isinstance(result, dict):
        return {}
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str) and not result.get("isError"):
            return result_object(text) or result
    return result


def result_is_error(result):
    if isinstance(result, str) and len(result) <= 2 * 1024 * 1024:
        try:
            result = json.loads(result)
        except (ValueError, TypeError, RecursionError):
            return False
    if isinstance(result, dict) and (result.get("isError") or result.get("is_error") or result.get("error") or result.get("success") is False):
        return True
    value = result_object(result)
    return bool(value.get("error") or value.get("isError") or value.get("is_error") or value.get("success") is False)


def _within_workspace(raw, root, cwd, *, exists=False):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("配置的路径字段无效")
    path = Path(raw)
    path = (path if path.is_absolute() else cwd / path).resolve(strict=exists)
    if not path.is_relative_to(root):
        raise ValueError("配置的路径字段超出当前工作区")
    return path


def _artifact_check(path, limit):
    size = path.stat().st_size
    if not path.is_file() or not size:
        raise ValueError("业务产物不存在或内容为空")
    if size > limit:
        raise ValueError("业务产物超过本次快速核验大小限制")
    office = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}
    suffix = path.suffix.lower()
    if suffix in office:
        with zipfile.ZipFile(path) as archive:
            archive.getinfo(office[suffix])
    elif suffix == ".pdf":
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("PDF 产物格式无效")
    elif suffix == ".json":
        if size > 1024 * 1024:
            raise ValueError("JSON 产物超过快速核验大小限制")
        json.loads(path.read_text(encoding="utf-8-sig"))


class BusinessExecution:
    """Shared by a WebUI turn and its children; no extra model calls or tool replay."""
    _CONTROL_TOOLS = {"delegate_task", "clarify", "todo", "todo_list"}

    def __init__(self, config, *, workspace, profile, session_id=None, on_blocked=None):
        self.policy = ExecutionPolicy.from_config(config)
        if not workspace or not Path(workspace).is_absolute() or not Path(workspace).is_dir():
            raise ValueError("业务检查需要明确、存在的绝对工作区目录")
        self.root = Path(workspace).resolve(strict=True)
        self.profile = profile
        self.session_id = session_id
        self.on_blocked = on_blocked
        self.allowed_tools = _names(config.get("allowed_tools"), "allowed_tools", optional=True)
        self.denied_tools = _names(config.get("denied_tools", []), "denied_tools")
        self.allowed_profiles = _names(config.get("allowed_profiles"), "allowed_profiles", optional=True)
        rules = config.get("tools", {})
        if not isinstance(rules, dict) or any(not isinstance(k, str) or not k for k in rules):
            raise ValueError("tools must be a mapping of actual tool names")
        self.rules = {name: ToolPolicy.from_config(rule) for name, rule in rules.items()}
        self._lock = threading.RLock()
        self._started = time.monotonic()
        self._calls = self._active = 0
        self._failures = {}
        self._records = {}

    @classmethod
    def from_config(cls, config, *, workspace, profile="default", session_id=None, on_blocked=None, api_mode=None):
        if config is None:
            return None
        if not isinstance(config, dict) or not isinstance(config.get("enabled", False), bool):
            raise ValueError("webui_execution_policy.enabled must be a boolean")
        if not config.get("enabled", False):
            return None
        if api_mode in {"acp", "codex_app_server", "claude_code"}:
            raise ValueError("业务检查仅支持 Hermes 原生工具执行模式")
        return cls(config, workspace=workspace, profile=profile, session_id=session_id, on_blocked=on_blocked)

    def snapshot(self, call_id):
        with self._lock:
            return dict(self._records.get(call_id, {"enabled": True, "permission": "pending"}))

    def _check(self, name, args, cwd):
        if self.allowed_profiles is not None and self.profile not in self.allowed_profiles:
            raise ValueError("当前 Profile 未获业务工具授权")
        if name in self.denied_tools or (self.allowed_tools is not None and name not in self.allowed_tools):
            raise ValueError("工具不在业务允许范围内")
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是对象")
        if len(json.dumps(args, ensure_ascii=False).encode("utf-8")) > self.policy.max_argument_bytes:
            raise ValueError("工具参数超过大小限制")
        rule = self.rules.get(name)
        if rule is None:
            return
        if rule.required_args - args.keys():
            raise ValueError("缺少业务规则要求的参数")
        types = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}
        for key, kind in rule.argument_types.items():
            if key in args and (not isinstance(args[key], types[kind]) or (kind in {"integer", "number"} and isinstance(args[key], bool))):
                raise ValueError("参数类型不符合业务规则")
        for key in rule.path_args:
            _within_workspace(args.get(key), self.root, cwd)

    def _admit(self, name, args, call_id, cwd):
        self._check(name, args, cwd)
        with self._lock:
            p = self.policy
            if p.time_budget_seconds and time.monotonic() - self._started >= p.time_budget_seconds:
                raise ValueError("本轮时间预算已用尽，停止后续工具调用")
            if p.max_tool_calls and self._calls >= p.max_tool_calls:
                raise ValueError("本轮工具调用次数已达到上限")
            failures = self._failures.get(name, 0)
            rule = self.rules.get(name)
            if failures and (not rule or not rule.retry_safe or failures > p.max_retries):
                raise ValueError("该业务步骤已停止重试，请向用户说明失败原因")
            worker = name not in self._CONTROL_TOOLS
            if worker and p.max_concurrent_calls and self._active >= p.max_concurrent_calls:
                raise ValueError("业务工具并发已达到上限，请等待在途任务完成后再调度")
            self._calls += 1
            self._active += int(worker)
            self._records[call_id] = {"enabled": True, "permission": "passed", "verification": "not_configured",
                                      "retry_count": failures, "max_retries": p.max_retries,
                                      "call_count": self._calls, "status": "running"}
            return worker

    def _verify(self, rule, result, cwd):
        value = result_object(result)
        if result_is_error(result):
            raise ValueError("业务工具返回失败结果")
        if rule.required_result_fields - value.keys():
            raise ValueError("业务结果缺少要求的字段")
        for key in rule.artifact_fields:
            path = _within_workspace(value.get(key), self.root, cwd, exists=True)
            _artifact_check(path, self.policy.max_artifact_bytes)

    def execute(self, name, args, *, execute, call_id, cwd, on_block, caller_session_id=None):
        try:
            cwd = cwd() if callable(cwd) else cwd
            cwd = Path(cwd or self.root).resolve()
            worker = self._admit(name, args, call_id, cwd)
        except (ValueError, TypeError, OSError) as exc:
            with self._lock:
                self._records[call_id] = {"enabled": True, "permission": "blocked", "status": "blocked",
                                          "reason": str(exc), "verification": "not_run"}
            result = on_block({"error": "business_policy_blocked", "message": str(exc), "retryable": False})
            # Native blocked calls intentionally skip complete callbacks. Emit only for
            # this owner, never project a child's tool into the parent's transcript.
            if self.on_blocked is not None and caller_session_id == self.session_id:
                self.on_blocked(call_id, name, args, result)
            return result
        started = time.monotonic()
        try:
            result = execute()
            rule = self.rules.get(name)
            if rule and (rule.required_result_fields or rule.artifact_fields):
                try:
                    self._verify(rule, result, cwd)
                except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
                    with self._lock:
                        self._failures[name] = self._failures.get(name, 0) + 1
                        again = rule.retry_safe and self._failures[name] <= self.policy.max_retries
                        reason = str(exc) if isinstance(exc, ValueError) else "业务产物缺失或格式不可读取"
                        self._records[call_id].update(verification="failed", status="failed", reason=reason, retryable=again)
                    return json.dumps({"error": "business_validation_failed", "message": reason,
                                       "retryable": again, "retry_count": self._failures[name] - 1,
                                       "max_retries": self.policy.max_retries,
                                       "expected_fields": sorted(rule.required_result_fields | rule.artifact_fields),
                                       "tool_result_preview": result[:4096] if isinstance(result, str) else "",
                                       "guidance": "请修正该步骤后再调用，不要重跑整个任务。" if again else "请停止此步骤并向用户说明原因。"}, ensure_ascii=False)
                with self._lock:
                    self._records[call_id]["verification"] = "passed"
                    self._failures.pop(name, None)
            with self._lock:
                self._records[call_id]["status"] = "failed" if result_is_error(result) else "completed"
            return result
        except BaseException:
            with self._lock:
                self._records[call_id].update(status="failed", reason="工具执行异常或已中断")
            raise
        finally:
            with self._lock:
                self._active -= int(worker)
                self._records[call_id]["elapsed_ms"] = round((time.monotonic() - started) * 1000)


def execution_detail(runtime, call_id):
    """Safe presentation-only metadata; never expose credentials or original arguments."""
    return runtime.snapshot(call_id) if runtime is not None else {"enabled": False}
