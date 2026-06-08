#!/usr/bin/env python3
import json
import os
import re
from typing import Dict, Any


def check_decision_branches(ref: Dict[str, Any], code_text: str) -> Dict[
  str, Any]:
  missing_branches_report: Dict[str, Any] = {}
  lines = code_text.splitlines()

  decision_conditions: Dict[str, list] = {}
  for decision_name, decision in ref.get("decisions", {}).items():
    clean_name = decision_name.rstrip("?").strip()
    conditions = [
      (e.get("condition") or "").strip() for e in ref.get("edges", [])
      if
      e.get("from", "").rstrip("?").strip() == clean_name and e.get("condition")
    ]
    conditions = [c for c in conditions if c]
    if conditions:
      decision_conditions[clean_name] = conditions.copy()

  patterns_by_decision: Dict[str, Dict[str, re.Pattern]] = {}
  for dec, conds in decision_conditions.items():
    patterns_by_decision[dec] = {
      cond: re.compile(rf"^\s*if\s+{re.escape(cond)}\s*:", flags=re.IGNORECASE)
      for cond in conds
    }

  decision_stack: list[tuple[str, int]] = []

  def leading_indent(s: str) -> int:
    cnt = 0
    for ch in s:
      if ch == " ":
        cnt += 1
      elif ch == "\t":
        cnt += 4
      else:
        break
    return cnt

  re_dec = re.compile(r"Decision\s*\(\s*([A-Za-z0-9_?]+)\s*\)")

  for raw_line in lines:
    line = raw_line.rstrip("\n")
    line_strip = line.strip()

    if not line_strip:
      continue

    m = re_dec.search(line_strip)
    if m:
      name = m.group(1).rstrip("?").strip()
      indent = leading_indent(line)

      decision_stack.append((name, indent))
      if name not in patterns_by_decision and name in decision_conditions:
        patterns_by_decision[name] = {
          cond: re.compile(rf"^\s*if\s+{re.escape(cond)}\s*:",
                           flags=re.IGNORECASE)
          for cond in decision_conditions[name]
        }
      continue

    current_indent = leading_indent(line)
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
      to_remove = []
      for cond in list(remaining):
        pat = patterns.get(cond)
        if pat and pat.match(line):
          to_remove.append(cond)
      for cond in to_remove:
        if cond in decision_conditions[dec_name]:
          decision_conditions[dec_name].remove(cond)
      continue
  for decision_name, remaining in decision_conditions.items():
    if remaining:
      missing_branches_report[decision_name] = remaining.copy()

  return missing_branches_report


def parse_comment_fields(comment: str) -> Dict[str, Any]:
  result = {}
  if not comment:
    return result
  for line in comment.split("\n"):
    line = line.strip()
    if not line or "=" not in line:
      continue
    k, v = line.split("=", 1)
    result[k.strip()] = v.strip()
  return result


# -------------------------------
# BUILD REFERENCE MODEL (fixed ? in decision names)
# -------------------------------
def build_reference_model(diagram_path: str) -> Dict[str, Any]:
  with open(diagram_path, "r", encoding="utf-8") as f:
    d = json.load(f)

  ref = {
    "activity": d.get("activity"),
    "actions": {},
    "decisions": {},
    "mergeNodes": {},
    "edges": []
  }

  for n in d["nodes"]:
    t = n.get("type", "")
    name = n["name"]

    if "CallBehaviorAction" in t:
      ref["actions"][name] = {
        "name": name,
        "comment": n.get("comment", ""),
        "props": parse_comment_fields(n.get("comment", "")),
      }

    if "DecisionNode" in t:
      # usuń znak ? na końcu nazwy
      clean_name = name.rstrip("?").strip()
      # usuń znak ? również w nazwach branchy
      branches = [x["target"].rstrip("?").strip() for x in n.get("next", [])]
      ref["decisions"][clean_name] = {
        "name": clean_name,
        "branches": branches
      }

    if "MergeNode" in t:
      ref["mergeNodes"][name] = {"name": name}

    for nxt in n.get("next", []):
      ref["edges"].append({
        "from": name,
        "to": nxt["target"].rstrip("?").strip(),
        "condition": nxt.get("condition")
      })

  return ref


def extract_code_model(code_dir: str) -> Dict[str, Any]:
  functions = {}
  calls = []
  decisions = []

  for root, _, files in os.walk(code_dir):
    for fname in files:
      if not fname.endswith((".txt", ".code", ".pseudo", ".py", ".md")):
        continue

      path = os.path.join(root, fname)
      with open(path, "r", encoding="utf-8") as f:
        text = f.read()

      # function declarations
      for m in re.finditer(r"function\s+([A-Za-z0-9_?]+)\s*\(", text):
        fun = m.group(1)
        functions[fun] = {"name": fun, "file": path}

      # regular calls
      for line in text.splitlines():
        if line.strip().startswith("function "):
          continue
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_? ]*)\s*\(\)", line):
          call_name = m.group(1).strip()
          calls.append(call_name)

      # Decision nodes
      for m in re.finditer(r"Decision\s*\(\s*([A-Za-z0-9_ ?]+)\s*\)", text):
        decisions.append(m.group(1).strip())

  return {
    "functions": functions,
    "calls": calls,
    "decisions": decisions
  }


def compare_models(ref: Dict[str, Any], code: Dict[str, Any]) -> Dict[str, Any]:
  report = {
    "missing_actions": [],
    "missing_decisions": [],
    "extra_actions": [],
    "unmatched_calls": [],
    "missing_decision_branches": {},
    "summary": {}
  }

  for action in ref["actions"]:
    if action not in code["calls"]:
      report["missing_actions"].append(action)

  for c in code["calls"]:
    # ignore main_* functions
    if c.startswith("main_"):
      continue

    # ignore merge nodes
    if c in ref.get("mergeNodes", {}):
      continue

    # ignore internal keywords
    if c in ("end",):
      continue

    # true extras
    if c not in ref["actions"]:
      report["extra_actions"].append(c)

  for d in ref["decisions"]:
    if d not in code["decisions"]:
      report["missing_decisions"].append(d)

  # --- Summary ---
  report["summary"] = {
    "total_reference_actions": len(ref["actions"]),
    "total_reference_decisions": len(ref["decisions"]),
    "found_calls": len(code["calls"]),
    "missing_actions_count": len(report["missing_actions"]),
    "missing_decisions_count": len(report["missing_decisions"]),
    "extra_actions_count": len(report["extra_actions"]),
    "missing_branches_count": 0  # aktualizacja poniżej
  }

  return report


def resolve_path(path: str) -> str:
  BASE_DIR = os.path.dirname(os.path.abspath(__file__))
  return os.path.abspath(os.path.join(BASE_DIR, "..", path))


def main():
  print("Loading diagram...")
  diagram_path = resolve_path(
    "files/activity_parsed/1_activity_with_conditions.json")
  reference = build_reference_model(diagram_path)

  print("Reading code...")
  code_dir = resolve_path('files/code/')
  code_model = extract_code_model(code_dir)

  code_file_path = os.path.join(code_dir, 'generated_plain_code.txt')
  with open(code_file_path, "r", encoding="utf-8") as f:
    code_text = f.read()

  print("Comparing...")
  report = compare_models(reference, code_model)

  print("Checking decision branches...")
  branch_report = check_decision_branches(reference, code_text)
  report["missing_decision_branches"] = branch_report
  report["summary"]["missing_branches_count"] = sum(
      len(v) for v in branch_report.values())

  report_path = resolve_path("reports/report_structural.json")
  os.makedirs(os.path.dirname(report_path), exist_ok=True)
  with open(report_path, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)

  print(f"Validation report saved to {report_path}")
  print(report)

  error_flag = False
  if report["missing_actions"]:
    print("ERROR: Missing actions detected!")
    error_flag = True
  if report["missing_decisions"]:
    print("ERROR: Missing decisions detected!")
    error_flag = True
  if report["extra_actions"]:
    print("ERROR: Extra actions detected!")
    error_flag = True
  if report["unmatched_calls"]:
    print("ERROR: Unmatched calls detected!")
    error_flag = True
  if report["missing_decision_branches"]:
    print("ERROR: Missing decision branches detected!")
    error_flag = True

  if not error_flag:
    print("All checks passed successfully!")


if __name__ == "__main__":
  main()
