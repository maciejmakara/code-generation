import json
from collections import defaultdict

INPUT_FILE = "files/activity_parsed/3_activity_with_steps.json"
OUTPUT_FILE = "files/activity_parsed/4_activity_optimized.json"


def group_by_suffix(steps):
  groups = defaultdict(list)
  for s in steps:
    step_id = s["step"]
    suffix = step_id[-1] if step_id[-1].isalpha() else "a"
    groups[suffix].append(s)
  return groups


def _same_node(a, b):
  if not a or not b:
    return False
  return a.get("action") == b.get("action") and a.get("type") == b.get("type")


def _common_prefix(paths):
  prefix = []
  i = 0
  while True:
    if any(i >= len(p) for p in paths):
      break
    first = paths[0][i]
    if all(_same_node(first, p[i]) for p in paths):
      prefix.append(first)
      i += 1
      continue
    break
  return prefix


def _common_suffix(paths):
  suffix = []
  i = 1
  while True:
    if any(i > len(p) for p in paths):
      break
    first = paths[0][-i]
    if all(_same_node(first, p[-i]) for p in paths):
      suffix.insert(0, first)
      i += 1
      continue
    break
  return suffix


def _strip_prefix(paths, n):
  return [p[n:] for p in paths]


def _strip_suffix(paths, n):
  if n <= 0:
    return paths
  return [p[:-n] for p in paths]


def build_optimized_flow(groups):
  paths = [p for p in groups.values() if p]
  if not paths:
    return []

  if len(paths) == 1:
    return list(paths[0])

  prefix = _common_prefix(paths)
  remaining = _strip_prefix(paths, len(prefix))

  if all(not p for p in remaining):
    return prefix

  suffix = _common_suffix([p for p in remaining if p] or remaining)
  remaining = _strip_suffix(remaining, len(suffix))

  last_common = prefix[-1] if prefix else None
  branch_container = None

  if last_common and last_common.get("type") == "uml:DecisionNode":
    prefix = prefix[:-1]
    branch_container = {
      "step": last_common.get("step"),
      "action": last_common.get("action"),
      "type": last_common.get("type"),
      "branches": []
    }

    branches = defaultdict(list)
    for p in remaining:
      if not p:
        branches["NoCondition"].append(p)
        continue
      key = p[0].get("condition") or "NoCondition"
      branches[key].append(p)

    for cond, branch_paths in branches.items():
      sub_groups = {str(i): bp for i, bp in enumerate(branch_paths)}
      sub_flow = build_optimized_flow(sub_groups)
      branch_container["branches"].append({
        "condition": cond,
        "path": sub_flow
      })

  elif last_common and last_common.get("type") == "uml:ForkNode":
    prefix = prefix[:-1]
    branch_container = {
      "step": last_common.get("step"),
      "action": last_common.get("action"),
      "type": last_common.get("type"),
      "branches": []
    }

    branches = defaultdict(list)
    for p in remaining:
      if not p:
        branches["Empty"].append(p)
        continue
      key = p[0].get("action")
      branches[key].append(p)

    for key, branch_paths in branches.items():
      sub_groups = {str(i): bp for i, bp in enumerate(branch_paths)}
      sub_flow = build_optimized_flow(sub_groups)
      branch_container["branches"].append({
        "start": key,
        "path": sub_flow
      })

  else:
    branch_container = {
      "type": "Branch",
      "branches": []
    }
    for i, p in enumerate(remaining):
      sub_groups = {"0": p}
      sub_flow = build_optimized_flow(sub_groups)
      branch_container["branches"].append({
        "id": str(i),
        "path": sub_flow
      })

  return prefix + [branch_container] + suffix

def remove_condition_from_actions(obj):
  if isinstance(obj, dict):
    if "action" in obj and "type" in obj:
      obj.pop("condition", None)
    for k, v in obj.items():
      remove_condition_from_actions(v)
  elif isinstance(obj, list):
    for item in obj:
      remove_condition_from_actions(item)
  return obj


if __name__ == "__main__":
  with open(INPUT_FILE, "r", encoding="utf-8") as f:
    model = json.load(f)

  groups = group_by_suffix(model["steps"])

  optimized_flow = build_optimized_flow(groups)

  data_clean = remove_condition_from_actions(optimized_flow)

  result = {"activity": model["activity"], "flow": data_clean}

  with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

  print(f"saved to {OUTPUT_FILE}")
