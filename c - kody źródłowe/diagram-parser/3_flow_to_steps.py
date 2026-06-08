import json
import sys

sys.setrecursionlimit(2000)

INPUT_FILE = "files/activity_parsed/2_activity_flow.json"
OUTPUT_FILE = "files/activity_parsed/3_activity_with_steps.json"


def build_map(flow):
  return {n["action"]: n for n in flow}


def find_start(flow):
  for n in flow:
    if n["type"] == "uml:InitialNode":
      return n
  raise ValueError("No InitialNode found")


def explore(node_name, nodes, path=None, condition=None, visited=None):
  if visited is None:
    visited = set()
  node = nodes[node_name]

  node_type = node["type"]
  if node_type in ("uml:JoinNode", "uml:MergeNode"):
    current_path = path if path else []
  else:
    step = {
      "action": node["action"],
      "type": node_type
    }
    if node.get("stereotype"):
      step["stereotype"] = node["stereotype"]
    if node.get("comment"):
      step["comment"] = node["comment"]
    if condition:
      step["condition"] = condition

    current_path = path + [step] if path else [step]

  if node_name in visited:
    loop_step = {
      "action": f"loop to {node_name}",
      "type": "Loop"
    }
    return [current_path + [loop_step]]

  visited.add(node_name)

  if node_type == "uml:ActivityFinalNode":
    return [current_path]

  if node_type == "uml:DecisionNode":
    all_paths = []
    for branch in node.get("branches", []):
      cond = branch.get("condition", "")
      target = branch["target"]
      if target not in nodes:
        continue
      all_paths += explore(target, nodes, current_path, cond, visited.copy())
    return all_paths

  nexts = node.get("next", [])
  if not nexts:
    return [current_path]

  all_paths = []
  for nxt in nexts:
    if nxt in nodes:
      all_paths += explore(nxt, nodes, current_path, None, visited.copy())
  return all_paths


def assign_step_labels(paths):
  steps = []
  step_num = 1

  for i, path in enumerate(paths):
    suffix = chr(96 + (i + 1)) if len(paths) > 1 else ""
    local_num = step_num

    for node in path:
      label = f"{local_num}{suffix}"
      steps.append({
        "step": label,
        **node
      })
      local_num += 1

    step_num = max(step_num, local_num)
  return steps


def transform(model):
  nodes = build_map(model["flow"])
  start = find_start(model["flow"])
  all_paths = explore(start["action"], nodes)
  steps = assign_step_labels(all_paths)
  return {"activity": model["activity"], "steps": steps}


if __name__ == "__main__":
  with open(INPUT_FILE, "r", encoding="utf-8") as f:
    model = json.load(f)

  result = transform(model)

  with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

  print(f"Saved to {OUTPUT_FILE}")
