import json

INPUT_FILE = "files/activity_parsed/1_activity_with_conditions.json"
OUTPUT_FILE = "files/activity_parsed/2_activity_flow.json"

def transform_flow(model):
  nodes_map = {node["name"]: node for node in model["nodes"]}
  start_node = next((n for n in model["nodes"] if n["type"] == "uml:InitialNode"), None)
  if not start_node:
    raise ValueError("no InitialNode found in model")

  visited = set()
  ordered_flow = []

  def traverse(node):
    if node["name"] in visited:
      return
    visited.add(node["name"])

    node_entry = {
      "action": node["name"],
      "type": node["type"]
    }
    if node.get("stereotype"):
      node_entry["stereotype"] = node["stereotype"]
    if node.get("comment"):
      node_entry["comment"] = node["comment"]

    if node["type"] == "uml:DecisionNode":
      branches = []
      for nxt in node.get("next", []):
        target_name = nxt["target"]
        branches.append({
          "condition": nxt.get("condition"),
          "target": target_name
        })
      node_entry["branches"] = branches
      ordered_flow.append(node_entry)

      for nxt in node.get("next", []):
        target_name = nxt["target"]
        if target_name in nodes_map:
          traverse(nodes_map[target_name])
    else:
      next_nodes = [n["target"] for n in node.get("next", []) if n.get("target")]
      node_entry["next"] = next_nodes
      ordered_flow.append(node_entry)

      for target_name in next_nodes:
        if target_name in nodes_map:
          traverse(nodes_map[target_name])

  traverse(start_node)

  return {
    "activity": model["activity"],
    "flow": ordered_flow
  }


if __name__ == "__main__":
  with open(INPUT_FILE, "r", encoding="utf-8") as f:
    model = json.load(f)

  transformed = transform_flow(model)

  with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(transformed, f, ensure_ascii=False, indent=2)

  print(f"saved to {OUTPUT_FILE}")
