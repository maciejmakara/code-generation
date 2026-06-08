import json
import argparse
from lxml import etree


def parse_visual_paradigm_activity(path, activity_index=None, activity_name=None):
  tree = etree.parse(path)
  root = tree.getroot()
  ns = root.nsmap

  activities = root.xpath(".//*[local-name()='packagedElement' and @xmi:type='uml:Activity']", namespaces=ns)
  if not activities:
    raise ValueError("Not found (packagedElement type uml:Activity).")

  selected = None
  if activity_name is not None:
    for a in activities:
      if a.get("name") == activity_name:
        selected = a
        break
    if selected is None:
      raise ValueError(f"Activity named '{activity_name}' not found. Available: {[a.get('name') for a in activities]}")
  else:
    idx = activity_index if activity_index is not None else 0
    if idx < 0 or idx >= len(activities):
      raise ValueError(f"Activity index {idx} out of range 0..{len(activities)-1}")
    selected = activities[idx]

  activity_name_resolved = selected.get("name", "UnnamedActivity")

  stereotypes = {}
  for el in root.xpath(".//*[namespace-uri() and contains(name(), ':')]", namespaces=ns):
    tag_name = etree.QName(el).localname
    for attr in el.attrib:
      if attr.startswith("base_"):
        base_attr = el.attrib[attr]
        stereotypes[base_attr] = tag_name

  nodes = {}
  for node in selected.xpath(".//*[local-name()='node']"):
    node_id = node.get("{http://schema.omg.org/spec/XMI/2.1}id") or node.get("xmi:id")
    node_type = node.get("{http://schema.omg.org/spec/XMI/2.1}type") or node.get("xmi:type")
    node_name = node.get("name", "").strip() or node_id

    stereotype = stereotypes.get(node_id)

    nodes[node_id] = {
      "id": node_id,
      "name": node_name,
      "type": node_type,
      "stereotype": stereotype,
      "comment": "",
      "next": []
    }

  comments = {}
  for comment in selected.xpath(".//*[local-name()='ownedComment']"):
    target = comment.get("annotatedElement")
    body = comment.get("body", "").replace("&#10;", "\n").strip()
    comments[target] = body

  for node_id, node in nodes.items():
    if node_id in comments:
      node["comment"] = comments[node_id]

  for edge in selected.xpath(".//*[local-name()='edge']"):
    src = edge.get("source")
    tgt = edge.get("target")
    condition = edge.get("name")
    if not src or not tgt:
      continue

    if src in nodes and tgt in nodes:
      target_name = nodes[tgt]["name"] or tgt
      nodes[src]["next"].append({
        "condition": condition if condition else None,
        "target": target_name
      })

  result = {
    "activity": activity_name_resolved,
    "nodes": list(nodes.values())
  }
  return result


def main():
  parser = argparse.ArgumentParser(description="Parse UML XMI activity non-interactively.")
  parser.add_argument("--uml", required=True, help="Path to UML XMI file")
  parser.add_argument("--out", required=True, help="Output JSON path")
  group = parser.add_mutually_exclusive_group()
  group.add_argument("--activity-index", type=int, help="Index of activity to parse")
  group.add_argument("--activity-name", help="Name of activity to parse")
  args = parser.parse_args()

  model = parse_visual_paradigm_activity(args.uml, args.activity_index, args.activity_name)
  with open(args.out, "w", encoding="utf-8") as f:
    json.dump(model, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
  main()
