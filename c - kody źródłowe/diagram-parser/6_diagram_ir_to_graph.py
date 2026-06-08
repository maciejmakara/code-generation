import json

INPUT_FILE = "files/activity_parsed/5_diagram_ir.json"
OUTPUT_DOT = "files/activity_parsed/6_diagram_ir.dot"
OUTPUT_MERMAID = "files/activity_parsed/6_diagram_ir.mmd"


def _escape(s: str) -> str:
  return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def _node_label(n: dict) -> str:
  action = n.get("action") or ""
  node_type = n.get("semanticType") or n.get("type") or ""
  return _escape(f"{action}\\n{node_type}")


def to_dot(model: dict) -> str:
  nodes = model.get("nodes", [])
  edges = model.get("edges", [])

  lines = [
    "digraph Activity {",
    "  rankdir=TB;",
    "  node [shape=box, fontname=Helvetica];",
    "  edge [fontname=Helvetica];",
  ]

  for n in nodes:
    node_id = n.get("action")
    if not node_id:
      continue

    node_type = n.get("type")
    shape = "box"
    if node_type == "uml:InitialNode":
      shape = "circle"
    elif node_type == "uml:ActivityFinalNode":
      shape = "doublecircle"
    elif node_type == "uml:DecisionNode":
      shape = "diamond"
    elif node_type == "uml:MergeNode":
      shape = "diamond"
    elif node_type == "uml:ForkNode":
      shape = "box"
    elif node_type == "uml:JoinNode":
      shape = "box"

    lines.append(f"  \"{_escape(node_id)}\" [label=\"{_node_label(n)}\", shape={shape}];")

  for e in edges:
    src = e.get("from")
    tgt = e.get("to")
    if not src or not tgt:
      continue

    label = e.get("condition")
    if label is None or label == "":
      lines.append(f"  \"{_escape(src)}\" -> \"{_escape(tgt)}\";")
    else:
      lines.append(f"  \"{_escape(src)}\" -> \"{_escape(tgt)}\" [label=\"{_escape(str(label))}\"]; ")

  lines.append("}")
  return "\n".join(lines)


def to_mermaid(model: dict) -> str:
  nodes = model.get("nodes", [])
  edges = model.get("edges", [])

  node_type_by_id = {n.get("action"): n.get("type") for n in nodes if n.get("action")}

  lines = ["flowchart TB"]

  for n in nodes:
    node_id = n.get("action")
    if not node_id:
      continue

    label = _node_label(n).replace("\\n", "<br/>")
    t = n.get("type")

    if t == "uml:DecisionNode" or t == "uml:MergeNode":
      lines.append(f"  {node_id}{{\"{label}\"}}")
    elif t == "uml:InitialNode":
      lines.append(f"  {node_id}((\"{label}\"))")
    elif t == "uml:ActivityFinalNode":
      lines.append(f"  {node_id}(((\"{label}\")))")
    else:
      lines.append(f"  {node_id}[\"{label}\"]")

  for e in edges:
    src = e.get("from")
    tgt = e.get("to")
    if not src or not tgt:
      continue
    cond = e.get("condition")
    if cond is None or cond == "":
      lines.append(f"  {src} --> {tgt}")
    else:
      lines.append(f"  {src} -- \"{_escape(str(cond))}\" --> {tgt}")

  return "\n".join(lines) + "\n"


if __name__ == "__main__":
  with open(INPUT_FILE, "r", encoding="utf-8") as f:
    model = json.load(f)

  dot = to_dot(model)
  with open(OUTPUT_DOT, "w", encoding="utf-8") as f:
    f.write(dot)

  mmd = to_mermaid(model)
  with open(OUTPUT_MERMAID, "w", encoding="utf-8") as f:
    f.write(mmd)

  print(f"Saved DOT to {OUTPUT_DOT}")
  print(f"Saved Mermaid to {OUTPUT_MERMAID}")
