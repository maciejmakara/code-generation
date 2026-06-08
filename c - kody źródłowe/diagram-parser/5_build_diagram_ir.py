import json

INPUT_FILE = "files/activity_parsed/1_activity_with_conditions.json"
OUTPUT_FILE = "files/activity_parsed/5_diagram_ir.json"


def build_ir(model: dict) -> dict:
  nodes = model.get("nodes", [])

  by_action = {}
  for n in nodes:
    action = n.get("name") or n.get("action") or n.get("id")
    by_action[action] = n

  edges = []
  for n in nodes:
    src_action = n.get("name") or n.get("action") or n.get("id")
    for e in n.get("next", []) or []:
      tgt_action = e.get("target")
      if not tgt_action:
        continue
      edges.append({
        "from": src_action,
        "to": tgt_action,
        "condition": e.get("condition"),
        "kind": "next",
      })

  in_deg = {k: 0 for k in by_action.keys()}
  out_deg = {k: 0 for k in by_action.keys()}

  for e in edges:
    if e["to"] in in_deg:
      in_deg[e["to"]] += 1
    else:
      in_deg[e["to"]] = 1

    if e["from"] in out_deg:
      out_deg[e["from"]] += 1
    else:
      out_deg[e["from"]] = 1

  ir_nodes = []
  for action, n in by_action.items():
    node_type = n.get("type")
    semantic_type = node_type
    if node_type == "uml:DecisionNode" and in_deg.get(action, 0) > 1:
      semantic_type = "uml:MergeDecisionNode"

    ir_node = {
      "id": n.get("id"),
      "action": action,
      "type": node_type,
      "semanticType": semantic_type,
      "stereotype": n.get("stereotype"),
      "comment": n.get("comment"),
      "in": in_deg.get(action, 0),
      "out": out_deg.get(action, 0),
    }

    ir_nodes.append(ir_node)

  ir_nodes.sort(key=lambda x: (x.get("type") != "uml:InitialNode", x.get("action") or ""))

  return {
    "activity": model.get("activity"),
    "nodes": ir_nodes,
    "edges": edges,
  }


if __name__ == "__main__":
  with open(INPUT_FILE, "r", encoding="utf-8") as f:
    model = json.load(f)

  result = build_ir(model)

  with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

  print(f"Saved to {OUTPUT_FILE}")
