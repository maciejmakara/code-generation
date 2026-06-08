import json
import os
import re

NODE_MAPPING = {
  "uml:InitialNode": lambda n: None,
  "uml:CallBehaviorAction": lambda n: f"{n['action']}()",
  "uml:ActivityFinalNode": lambda n: "end()"
}


def _reachable_join_distances(start_action: str, index: dict) -> dict:

  distances = {}
  queue = [(start_action, 0)]
  seen = set()

  while queue:
    current, dist = queue.pop(0)
    if current in seen:
      continue
    seen.add(current)

    node = index.get(current)
    if not node:
      continue

    if node.get("type") == "uml:JoinNode":
      if current not in distances or dist < distances[current]:
        distances[current] = dist
      continue

    node_type = node.get("type")
    outgoing = []
    if node_type == "uml:DecisionNode":
      outgoing = [b.get("target") for b in node.get("branches", []) if b.get("target")]
    else:
      outgoing = node.get("next", [])

    for nxt in outgoing:
      queue.append((nxt, dist + 1))

  return distances


def _find_common_join_for_fork(fork_node: dict, index: dict):

  branch_starts = fork_node.get("next", [])
  if not branch_starts:
    return None

  per_branch = [_reachable_join_distances(s, index) for s in branch_starts]
  if any(not d for d in per_branch):
    return None

  common = set(per_branch[0].keys())
  for d in per_branch[1:]:
    common &= set(d.keys())
  if not common:
    return None

  def score(join_action: str) -> int:
    return sum(d[join_action] for d in per_branch)

  return min(common, key=score)

def normalize_comment_text(raw: str) -> str:

  raw = raw.replace("\r", "")
  lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
  return " | ".join(lines)


def parse_comment_to_json(raw: str):
  def _split_kv_blocks(text: str) -> dict:
    blocks = {}
    current_key = None
    current_value_lines = []

    for raw_ln in text.replace("\r", "").split("\n"):
      ln = raw_ln.strip()
      if not ln:
        continue

      m = re.match(r"^([A-Za-z0-9_]+)\s*=\s*(.*)$", ln)
      if m:
        if current_key is not None:
          blocks[current_key] = "\n".join(current_value_lines).strip()
        current_key = m.group(1)
        current_value_lines = [m.group(2)] if m.group(2) else []
        continue

      if current_key is not None:
        current_value_lines.append(ln)

    if current_key is not None:
      blocks[current_key] = "\n".join(current_value_lines).strip()

    return blocks

  def _cleanup_json_like(s: str) -> str:
    # Remove trailing commas before closing brackets/braces to make JSON parseable.
    s = re.sub(r",\s*([\]\}])", r"\1", s)
    return s

  def _parse_value(value: str):
    value = value.strip()
    if not value:
      return ""

    if value.isdigit():
      return int(value)

    if value.lower() == "true":
      return True
    if value.lower() == "false":
      return False

    m = re.match(r"([a-zA-Z_]+)\((\d+)\)", value)
    if m:
      return {"type": m.group(1), "value": int(m.group(2))}

    json_candidate = value
    if json_candidate.startswith("[") or json_candidate.startswith("{"):
      try:
        return json.loads(_cleanup_json_like(json_candidate))
      except Exception:
        pass

    return value.strip('"').strip("'")

  meta = {}
  blocks = _split_kv_blocks(raw)
  for key, value in blocks.items():
    meta[key] = _parse_value(value)

  return meta


def format_comment(node):
  meta = {}

  if node.get("stereotype"):
    meta["stereotype"] = node["stereotype"]

  if node.get("comment"):
    parsed_comment = parse_comment_to_json(node["comment"])
    meta.update(parsed_comment)

  if not meta:
    return ""

  return "  # @meta " + json.dumps(meta, ensure_ascii=False)


def process_node(
    node,
    index,
    merge_functions,
    indent=1,
    visited=None,
    loopback_decision=None,
    stop_at_action=None,
):
  if visited is None:
    visited = set()

  code = []
  spc = "  " * indent
  node_type = node["type"]
  action = node["action"]

  if action in visited:
    return code
  visited.add(action)

  if stop_at_action and action == stop_at_action:
    return code

  if node_type in ("uml:CallBehaviorAction", "uml:ActivityFinalNode"):
    line = spc + NODE_MAPPING[node_type](node)
    line += format_comment(node)
    code.append(line)
    for nxt in node.get("next", []):
      if (
          loopback_decision
          and node_type == "uml:CallBehaviorAction"
          and nxt == loopback_decision.get("action")
      ):
        action_name = f"ok{action.lower()}"
        code.append(
            spc
            + f"BackToDecision({loopback_decision.get('name')})({action_name})"
        )
        continue

      code.extend(
          process_node(
              index[nxt],
              index,
              merge_functions,
              indent,
              visited.copy(),
              loopback_decision=None,
              stop_at_action=stop_at_action,
          )
      )
    return code

  # --- DECISION ---
  elif node_type == "uml:DecisionNode":
    decision_name = node["action"].replace("?", "")  # usuwamy '?'
    code.append(spc + f"Decision({decision_name})")

    for branch in node.get("branches", []):
      cond = branch.get("condition", "unknown").lower()   # yes/no lowercase
      target = branch.get("target")
      if target:
        code.append(spc + f"if {cond}:")
        code.extend(process_node(
            index[target],
            index,
            merge_functions,
            indent + 1,
            visited.copy(),
            loopback_decision={"action": node["action"], "name": decision_name, "cond": cond},
            stop_at_action=stop_at_action,
        ))
    return code

  # --- FORK / JOIN ---
  elif node_type == "uml:ForkNode":
    join_action = _find_common_join_for_fork(node, index)
    code.append(spc + f"Fork({action})")

    for branch_start in node.get("next", []):
      code.append(spc + f"parallel {branch_start}:")
      code.extend(
          process_node(
              index[branch_start],
              index,
              merge_functions,
              indent + 1,
              visited.copy(),
              loopback_decision=None,
              stop_at_action=join_action,
          )
      )

    if join_action and join_action in index:
      code.append(spc + f"Join({join_action})")
      for nxt in index[join_action].get("next", []):
        code.extend(
            process_node(
                index[nxt],
                index,
                merge_functions,
                indent,
                visited.copy(),
                loopback_decision=None,
                stop_at_action=stop_at_action,
            )
        )

    return code

  elif node_type == "uml:JoinNode":
    for nxt in node.get("next", []):
      code.extend(
          process_node(
              index[nxt],
              index,
              merge_functions,
              indent,
              visited.copy(),
              loopback_decision=None,
              stop_at_action=stop_at_action,
          )
      )
    return code

  # --- MERGE NODE ---
  elif node_type == "uml:MergeNode":
    if action not in merge_functions:
      merge_functions[action] = [f"function {action}():"]
      for nxt in node.get("next", []):
        merge_functions[action].extend(
            process_node(
                index[nxt],
                index,
                merge_functions,
                indent=1,
                visited=set(),
                stop_at_action=stop_at_action,
            )
        )
      if len(merge_functions[action]) == 1:
        merge_functions[action].append("  return")
    code.append(spc + f"{action}()")
    return code

  return code


def generate_pseudocode(flow_json):
  index = {node["action"]: node for node in flow_json["flow"]}
  merge_functions = {}

  main_func_name = flow_json.get("activity", "MainActivity").replace(" ", "")
  initial_nodes = [n for n in flow_json["flow"] if n["type"] == "uml:InitialNode"]
  code = [f"REST DEFINITION {format_comment(initial_nodes[0])}\n\nfunction main_{main_func_name}():"]

  for n in initial_nodes:
    for nxt in n.get("next", []):
      code.extend(process_node(index[nxt], index, merge_functions))

  # MERGE nodes → function MergeNodeX():
  for func_code in merge_functions.values():
    code.append("")
    code.extend(func_code)

  return "\n".join(code)


if __name__ == "__main__":
  base_dir = os.path.dirname(os.path.abspath(__file__))
  input_file = os.path.join(base_dir, "files", "activity_parsed", "2_activity_flow.json")
  with open(input_file, encoding="utf-8") as f:
    flow_json = json.load(f)

  pseudocode = generate_pseudocode(flow_json)
  print(pseudocode)

  output_file = os.path.join(base_dir, "files", "code", "generated_plain_code.txt")
  with open(output_file, "w", encoding="utf-8") as f:
    f.write(pseudocode)
