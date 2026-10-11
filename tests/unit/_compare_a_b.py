"""对比 test_v3_a_explicit.log vs test_v3_b_default.log

唯一变量: a 显式设 SMARTBUTLER_MEMORY_JUDGE_ON_MESSAGE=true
        b 不设, 走 .env 默认值
"""
from pathlib import Path

a = Path("test_v3_a_explicit.log").read_text(encoding="utf-8", errors="replace").splitlines()
b = Path("test_v3_b_default.log").read_text(encoding="utf-8", errors="replace").splitlines()

print(f"lines_a={len(a)}  lines_b={len(b)}  diff={len(a)-len(b)}")
print()

def count(pat, lines):
    return sum(1 for ln in lines if pat in ln)

PATS = [
    "task_scheduler.started",
    "task_scheduler.segment_call",
    "task_scheduler.scan_start",
    "task_scheduler.scan_done",
    "task_scheduler.debounce_fire",
    "task_scheduler.on_message",
    "task_segmenter.judge_prompt",
    "task_segmenter.judge_response",
    "task_segmenter.judge_parsed",
    "task_segmenter.llm_failed",
    "task_segmenter.segment_enter",
    "task_index.completed",
    "task_index.paused",
    "task_index.started",
    "actions=",
    "t-",  # 出现过的 task_id 总数
    "auto_extract",
    ">>> [",
    "<<< ",
    "[实验结束]",
    "通过:",
    "期望:",
    "重复",
]

print(f"{'pattern':<32} {'a':>5} {'b':>5} {'Δ':>5}")
for p in PATS:
    ca, cb = count(p, a), count(p, b)
    flag = "  <-- " if ca != cb else ""
    print(f"{p:<32} {ca:>5} {cb:>5} {ca-cb:>+5}{flag}")

# 提取 task_id 集合(两种 log 各自的"事件指纹")
import re
def taskids(lines):
    return sorted(set(re.findall(r"t-[0-9a-f]{12,}", "".join(lines))))

ids_a = taskids(a)
ids_b = taskids(b)
print()
print(f"unique task_ids a={len(ids_a)}  b={len(ids_b)}")
print(f"only_in_a={set(ids_a)-set(ids_b)}")
print(f"only_in_b={set(ids_b)-set(ids_a)}")
print(f"common   ={len(set(ids_a)&set(ids_b))}")
