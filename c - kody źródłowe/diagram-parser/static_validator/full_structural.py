#!/usr/bin/env python3
import json
import os
import re
from typing import Any, Dict, List, Set, Tuple


def _load_json(path: str) -> Any:
  with open(path, "r", encoding="utf-8") as f:
    return json.load(f)


def resolve_path(path: str) -> str:
  base_dir = os.path.dirname(os.path.abspath(__file__))
  return os.path.abspath(os.path.join(base_dir, "..", path))


def _build_adjacency(edges: List[Dict[str, Any]]) -> Dict[str, List[str]]:
  adj: Dict[str, List[str]] = {}
  for e in edges:
    src = e.get("from")
    tgt = e.get("to")
    if not src or not tgt:
      continue
    adj.setdefault(src, []).append(tgt)
  return adj


def _reachable_join_distances(start_action: str, join_actions: Set[str], adj: Dict[str, List[str]]) -> Dict[str, int]:
  distances: Dict[str, int] = {}
  queue: List[Tuple[str, int]] = [(start_action, 0)]
  seen: Set[str] = set()

  while queue:
    current, dist = queue.pop(0)
    if current in seen:
      continue
    seen.add(current)

    if current in join_actions:
      prev = distances.get(current)
      if prev is None or dist < prev:
        distances[current] = dist
      continue

    for nxt in adj.get(current, []):
      queue.append((nxt, dist + 1))

  return distances


def _find_common_join_for_fork(fork_action: str, fork_next: List[str], join_actions: Set[str], adj: Dict[str, List[str]]) -> str | None:
  if not fork_next:
    return None

  per_branch = [_reachable_join_distances(s, join_actions, adj) for s in fork_next]
  if any(not d for d in per_branch):
    return None

  common = set(per_branch[0].keys())
  for d in per_branch[1:]:
    common &= set(d.keys())
  if not common:
    return None

  def score(join_action: str) -> int:
    return sum(d.get(join_action, 10**9) for d in per_branch)

  return min(common, key=score)


def build_reference_model_from_ir(diagram_ir_path: str) -> Dict[str, Any]:
  d = _load_json(diagram_ir_path)

  nodes = d.get("nodes", [])
  edges = d.get("edges", [])

  ref: Dict[str, Any] = {
    "activity": d.get("activity"),
    "actions": set(),
    "decisions": set(),
    "mergeDecisions": set(),
    "mergeNodes": set(),
    "forks": set(),
    "joins": set(),
    "edges": edges,
    "fork_to_join": {},
  }

  node_by_action: Dict[str, Dict[str, Any]] = {}
  for n in nodes:
    action = n.get("action")
    if action:
      node_by_action[action] = n

  for action, n in node_by_action.items():
    t = n.get("type") or ""
    semantic_t = n.get("semanticType") or t

    if "CallBehaviorAction" in t:
      ref["actions"].add(action)

    if "DecisionNode" in t:
      ref["decisions"].add(action.rstrip("?").strip())
      if semantic_t == "uml:MergeDecisionNode":
        ref["mergeDecisions"].add(action.rstrip("?").strip())

    if "MergeNode" in t:
      ref["mergeNodes"].add(action)

    if "ForkNode" in t:
      ref["forks"].add(action)

    if "JoinNode" in t:
      ref["joins"].add(action)

  adj = _build_adjacency(edges)
  join_actions = set(ref["joins"])  # type: ignore

  for fork_action in sorted(ref["forks"]):  # type: ignore
    fork_next = adj.get(fork_action, [])
    join_action = _find_common_join_for_fork(fork_action, fork_next, join_actions, adj)
    if join_action:
      ref["fork_to_join"][fork_action] = join_action

  return ref


def _is_disabled_line(line: str) -> bool:
  s = line.strip()
  # In generated_plain_code.txt you often "remove" lines by prefixing with '-'
  # (diff style). These should not count as real code.
  return (not s) or s.startswith(("-", "#", "//", ";"))


def extract_code_model_from_text(text: str) -> Dict[str, Any]:
  calls: List[str] = []
  decisions: List[str] = []
  forks: List[str] = []
  joins: List[str] = []
  back_to_decisions: List[str] = []

  for line in text.splitlines():
    if _is_disabled_line(line):
      continue
    if line.strip().startswith("function "):
      continue
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_? ]*)\s*\(\)", line):
      calls.append(m.group(1).strip())

    # Loop-back markers to decision nodes
    for m in re.finditer(r"\bBackToDecision\s*\(\s*([A-Za-z0-9_?]+)\s*\)", line):
      back_to_decisions.append(m.group(1).rstrip("?").strip())

  for m in re.finditer(r"Decision\s*\(\s*([A-Za-z0-9_ ?]+)\s*\)", text):
    decisions.append(m.group(1).strip().rstrip("?").strip())

  for m in re.finditer(r"Fork\s*\(\s*([A-Za-z0-9_ ?]+)\s*\)", text):
    forks.append(m.group(1).strip())

  for m in re.finditer(r"Join\s*\(\s*([A-Za-z0-9_ ?]+)\s*\)", text):
    joins.append(m.group(1).strip())

  return {
    "calls": calls,
    "decisions": decisions,
    "forks": forks,
    "joins": joins,
    "back_to_decisions": back_to_decisions,
  }


def extract_code_model(code_file_path: str) -> Dict[str, Any]:
  with open(code_file_path, "r", encoding="utf-8") as f:
    text = f.read()
  return extract_code_model_from_text(text)


def _leading_indent(s: str) -> int:
  cnt = 0
  for ch in s:
    if ch == " ":
      cnt += 1
    elif ch == "\t":
      cnt += 4
    else:
      break
  return cnt


def check_fork_join_blocks(code_text: str, fork_to_join: Dict[str, str]) -> List[Dict[str, Any]]:
  issues: List[Dict[str, Any]] = []
  lines = code_text.splitlines()

  re_fork = re.compile(r"^\s*Fork\(\s*([^\)]+)\s*\)\s*$")
  re_join = re.compile(r"^\s*Join\(\s*([^\)]+)\s*\)\s*$")
  re_parallel = re.compile(r"^\s*parallel\s+([^:]+)\s*:\s*$", flags=re.IGNORECASE)

  i = 0
  while i < len(lines):
    line = lines[i]
    m_fork = re_fork.match(line)
    if not m_fork:
      i += 1
      continue

    fork_action = m_fork.group(1).strip()
    fork_indent = _leading_indent(line)
    expected_join = fork_to_join.get(fork_action)

    j = i + 1
    saw_parallel = False
    saw_join = False

    # Scan forward until we leave the fork block (indent < fork indent)
    while j < len(lines):
      l2 = lines[j]
      if not l2.strip():
        j += 1
        continue

      ind2 = _leading_indent(l2)
      if ind2 < fork_indent:
        break

      # Join must be at same indent as Fork
      m_join = re_join.match(l2)
      if m_join and ind2 == fork_indent:
        join_action = m_join.group(1).strip()
        saw_join = True
        if expected_join and join_action != expected_join:
          issues.append({
            "fork": fork_action,
            "issue": "wrong_join",
            "expected_join": expected_join,
            "found_join": join_action,
            "line": j + 1,
          })
        break

      # If we see Join inside a parallel block (indent > fork indent), that's structurally wrong
      if m_join and ind2 > fork_indent:
        issues.append({
          "fork": fork_action,
          "issue": "join_inside_parallel_block",
          "expected_join": expected_join,
          "found_join": m_join.group(1).strip(),
          "line": j + 1,
        })
        # Keep scanning; there still might be a correct join later
        j += 1
        continue

      # Record that fork created parallel blocks; we require at least one
      if re_parallel.match(l2) and ind2 == fork_indent:
        saw_parallel = True

      j += 1

    if not saw_parallel:
      issues.append({
        "fork": fork_action,
        "issue": "missing_parallel_blocks",
        "expected_join": expected_join,
        "line": i + 1,
      })

    if expected_join and not saw_join:
      issues.append({
        "fork": fork_action,
        "issue": "missing_join",
        "expected_join": expected_join,
        "line": i + 1,
      })

    # Continue from where we stopped scanning (or next line)
    i = max(i + 1, j)

  return issues


def check_decision_branches(ref_edges: List[Dict[str, Any]], ref_decisions: Set[str], code_text: str) -> Dict[str, List[str]]:
  missing_branches_report: Dict[str, List[str]] = {}
  lines = code_text.splitlines()

  decision_conditions: Dict[str, List[str]] = {}
  for d in ref_decisions:
    clean_name = d.rstrip("?").strip()
    conditions = [
      (e.get("condition") or "").strip() for e in ref_edges
      if e.get("from", "").rstrip("?").strip() == clean_name and e.get("condition")
    ]
    conditions = [c for c in conditions if c]
    if conditions:
      decision_conditions[clean_name] = conditions.copy()

  patterns_by_decision: Dict[str, Dict[str, re.Pattern]] = {}
  for dec, conds in decision_conditions.items():
    patterns_by_decision[dec] = {
      cond: re.compile(rf"^\s*if\s+{re.escape(cond)}\s*:\s*$", flags=re.IGNORECASE)
      for cond in conds
    }

  decision_stack: List[Tuple[str, int]] = []

  re_dec = re.compile(r"Decision\s*\(\s*([A-Za-z0-9_?]+)\s*\)")

  for raw_line in lines:
    line = raw_line.rstrip("\n")
    line_strip = line.strip()
    if not line_strip:
      continue

    m = re_dec.search(line_strip)
    if m:
      name = m.group(1).rstrip("?").strip()
      indent = _leading_indent(line)
      decision_stack.append((name, indent))
      if name not in patterns_by_decision and name in decision_conditions:
        patterns_by_decision[name] = {
          cond: re.compile(rf"^\s*if\s+{re.escape(cond)}\s*:\s*$", flags=re.IGNORECASE)
          for cond in decision_conditions[name]
        }
      continue

    current_indent = _leading_indent(line)
    while decision_stack and current_indent < decision_stack[-1][1]:
      decision_stack.pop()

    if not decision_stack:
      continue

    if line_strip.startswith("Decision("):
      continue

    if re.match(r"^\s*if\s+", line):
      dec_name = decision_stack[-1][0]
      if dec_name not in decision_conditions:
        continue

      remaining = set(decision_conditions[dec_name])
      patterns = patterns_by_decision.get(dec_name, {})
      to_remove: List[str] = []
      for cond in list(remaining):
        pat = patterns.get(cond)
        if pat and pat.match(line):
          to_remove.append(cond)
      for cond in to_remove:
        if cond in decision_conditions[dec_name]:
          decision_conditions[dec_name].remove(cond)

  for decision_name, remaining in decision_conditions.items():
    if remaining:
      missing_branches_report[decision_name] = remaining.copy()

  return missing_branches_report


def compare_models(ref: Dict[str, Any], code: Dict[str, Any]) -> Dict[str, Any]:
  ref_actions: Set[str] = set(ref.get("actions", set()))
  ref_decisions: Set[str] = set(ref.get("decisions", set()))
  ref_merge_decisions: Set[str] = set(ref.get("mergeDecisions", set()))
  ref_merge_nodes: Set[str] = set(ref.get("mergeNodes", set()))
  ref_forks: Set[str] = set(ref.get("forks", set()))
  ref_joins: Set[str] = set(ref.get("joins", set()))
  ref_edges: List[Dict[str, Any]] = list(ref.get("edges", []))

  code_calls_list = list(code.get("calls", []))
  code_calls = set(code_calls_list)
  code_decisions = set(code.get("decisions", []))
  code_forks = set(code.get("forks", []))
  code_joins = set(code.get("joins", []))
  code_back_to_decisions_list = list(code.get("back_to_decisions", []))
  code_back_to_decisions = set(code_back_to_decisions_list)

  missing_actions = sorted([a for a in ref_actions if a not in code_calls])
  extra_actions = sorted([
    c for c in code_calls
    if c not in ref_actions
       and not c.startswith("main_")
       and not c.startswith("FlowJoin")
       and c not in ("end",)
  ])

  missing_decisions = sorted([d for d in ref_decisions if d not in code_decisions])
  missing_merge_decisions = sorted([d for d in ref_merge_decisions if d not in code_decisions])

  # Merge nodes (FlowJoin*) are structural control-flow nodes; we require correct multiplicity in code
  # based on how many incoming edges they have in the IR.
  required_merge_node_counts: Dict[str, int] = {}
  for e in ref_edges:
    tgt = (e.get("to") or "").rstrip("?").strip()
    if tgt in ref_merge_nodes:
      required_merge_node_counts[tgt] = required_merge_node_counts.get(tgt, 0) + 1

  missing_merge_nodes: Dict[str, Dict[str, int]] = {}
  for merge_node, required_count in required_merge_node_counts.items():
    found = code_calls_list.count(merge_node)
    if found < required_count:
      missing_merge_nodes[merge_node] = {"required": required_count, "found": found}

  # Loop-back edges into decision nodes should be represented in code via BackToDecision(<decision>)
  # We require the correct multiplicity based on how many such loop-back edges exist.
  required_back_to_decision_counts: Dict[str, int] = {}
  # Build decision -> next targets map to detect actual cycles back into a decision.
  decision_to_next: Dict[str, Set[str]] = {}
  for e in ref_edges:
    src = (e.get("from") or "").rstrip("?").strip()
    tgt = (e.get("to") or "").rstrip("?").strip()
    if src in ref_decisions or src in ref_merge_decisions:
      if tgt:
        decision_to_next.setdefault(src, set()).add(tgt)

  for e in ref_edges:
    src = (e.get("from") or "").rstrip("?").strip()
    tgt = (e.get("to") or "").rstrip("?").strip()
    if not src or not tgt:
      continue
    if tgt in ref_merge_decisions or tgt in ref_decisions:
      # We only treat it as a "back" if it originates from a non-decision node
      # (e.g., CreateUserAccount -> isTimeout)
      if src not in ref_decisions and src not in ref_merge_decisions:
        # And only if it forms an actual cycle: decision -> src exists.
        # Plus: the source should be a real action (CallBehaviorAction), not
        # a control node or the initial node.
        if src in ref_actions and src in decision_to_next.get(tgt, set()):
          required_back_to_decision_counts[tgt] = required_back_to_decision_counts.get(tgt, 0) + 1

  missing_back_to_decisions: Dict[str, Dict[str, int]] = {}
  for decision, required_count in required_back_to_decision_counts.items():
    found = code_back_to_decisions_list.count(decision)
    if found < required_count:
      missing_back_to_decisions[decision] = {"required": required_count, "found": found}

  missing_forks = sorted([f for f in ref_forks if f not in code_forks])
  missing_joins = sorted([j for j in ref_joins if j not in code_joins])

  fork_join_mismatches: List[Dict[str, Any]] = []
  fork_to_join: Dict[str, str] = ref.get("fork_to_join", {})
  for fork_action, join_action in fork_to_join.items():
    expected = {"fork": fork_action, "expected_join": join_action}
    expected["fork_in_code"] = fork_action in code_forks
    expected["join_in_code"] = join_action in code_joins
    if not expected["fork_in_code"] or not expected["join_in_code"]:
      fork_join_mismatches.append(expected)

  report = {
    "missing_actions": missing_actions,
    "missing_decisions": missing_decisions,
    "missing_merge_decisions": missing_merge_decisions,
    "missing_merge_nodes": missing_merge_nodes,
    "missing_back_to_decisions": missing_back_to_decisions,
    "missing_forks": missing_forks,
    "missing_joins": missing_joins,
    "extra_actions": extra_actions,
    "missing_decision_branches": {},
    "fork_join_mismatches": fork_join_mismatches,
    "fork_join_block_issues": [],
    "summary": {
      "total_reference_actions": len(ref_actions),
      "total_reference_decisions": len(ref_decisions),
      "total_reference_merge_decisions": len(ref_merge_decisions),
      "total_reference_merge_nodes": len(ref_merge_nodes),
      "total_reference_forks": len(ref_forks),
      "total_reference_joins": len(ref_joins),
      "found_calls": len(code_calls_list),
      "found_decisions": len(code_decisions),
      "found_forks": len(code_forks),
      "found_joins": len(code_joins),
      "missing_actions_count": len(missing_actions),
      "missing_decisions_count": len(missing_decisions),
      "missing_merge_decisions_count": len(missing_merge_decisions),
      "missing_merge_nodes_count": len(missing_merge_nodes),
      "missing_back_to_decisions_count": len(missing_back_to_decisions),
      "missing_forks_count": len(missing_forks),
      "missing_joins_count": len(missing_joins),
      "extra_actions_count": len(extra_actions),
      "missing_branches_count": 0,
      "fork_join_mismatches_count": len(fork_join_mismatches),
      "fork_join_block_issues_count": 0,
    },
  }

  return report


def main():
  diagram_ir_path = resolve_path("files/activity_parsed/5_diagram_ir.json")
  reference = build_reference_model_from_ir(diagram_ir_path)

  code_dir = resolve_path("files/code/")
  code_file_path = os.path.join(code_dir, "generated_plain_code.txt")
  code_model = extract_code_model(code_file_path)
  with open(code_file_path, "r", encoding="utf-8") as f:
    code_text = f.read()

  report = compare_models(reference, code_model)

  branch_report = check_decision_branches(reference.get("edges", []), set(reference.get("decisions", set())), code_text)
  report["missing_decision_branches"] = branch_report
  report["summary"]["missing_branches_count"] = sum(len(v) for v in branch_report.values())

  block_issues = check_fork_join_blocks(code_text, reference.get("fork_to_join", {}))
  report["fork_join_block_issues"] = block_issues
  report["summary"]["fork_join_block_issues_count"] = len(block_issues)

  report_path = resolve_path("reports/report_full_structural.json")
  os.makedirs(os.path.dirname(report_path), exist_ok=True)
  with open(report_path, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)

  print(f"Validation report saved to {report_path}")

  error_flag = False
  if report["missing_actions"]:
    print("ERROR: Missing actions detected!")
    error_flag = True
  if report["missing_decisions"]:
    print("ERROR: Missing decisions detected!")
    error_flag = True
  if report["missing_merge_decisions"]:
    print("ERROR: Missing merge-decision nodes detected!")
    error_flag = True
  if report.get("missing_merge_nodes"):
    print("ERROR: Missing merge nodes (FlowJoin*) detected!")
    error_flag = True
  if report.get("missing_back_to_decisions"):
    print("ERROR: Missing BackToDecision(<decision>) loopbacks detected!")
    error_flag = True
  if report["missing_forks"]:
    print("ERROR: Missing forks detected!")
    error_flag = True
  if report["missing_joins"]:
    print("ERROR: Missing joins detected!")
    error_flag = True
  if report["fork_join_mismatches"]:
    print("ERROR: Fork/join mismatches detected!")
    error_flag = True
  if report["fork_join_block_issues"]:
    print("ERROR: Fork/join block correctness issues detected!")
    error_flag = True

  if report["missing_decision_branches"]:
    print("ERROR: Missing decision branches detected!")
    error_flag = True

  if not error_flag:
    print("All checks passed successfully!")


if __name__ == "__main__":
  main()
