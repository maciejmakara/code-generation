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


def _is_disabled_line(line: str) -> bool:
  s = line.strip()
  return (not s) or s.startswith(("-", "#", "//", ";"))


def _normalize_name(name: str) -> str:
  return name.rstrip("?").strip()


def _parse_functions(code_text: str) -> Tuple[Dict[str, List[Tuple[str, int]]], Dict[str, int]]:
  """Parse code into per-function token lists.

  Returns:
    - tokens_by_func: function_name -> list of (token_name, line_number)
    - func_decl_line: function_name -> line_number of the function declaration
  """
  tokens_by_func: Dict[str, List[Tuple[str, int]]] = {}
  func_decl_line: Dict[str, int] = {}

  re_call = re.compile(r"\b([A-Za-z_][A-Za-z0-9_? ]*)\s*\(\)")
  re_decision = re.compile(r"Decision\s*\(\s*([A-Za-z0-9_? ]+)\s*\)")
  re_fork = re.compile(r"Fork\s*\(\s*([A-Za-z0-9_? ]+)\s*\)")
  re_join = re.compile(r"Join\s*\(\s*([A-Za-z0-9_? ]+)\s*\)")
  re_back_to_decision = re.compile(r"\bBackToDecision\s*\(\s*([A-Za-z0-9_?]+)\s*\)")
  re_func = re.compile(r"^\s*function\s+([A-Za-z0-9_?]+)\s*\(")

  current_func = "<global>"
  tokens_by_func.setdefault(current_func, [])

  for idx, raw in enumerate(code_text.splitlines(), start=1):
    if _is_disabled_line(raw):
      continue
    line = raw.rstrip("\n")

    m_func = re_func.match(line)
    if m_func:
      current_func = _normalize_name(m_func.group(1))
      tokens_by_func.setdefault(current_func, [])
      func_decl_line[current_func] = idx
      continue

    for m in re_decision.finditer(line):
      tokens_by_func[current_func].append((_normalize_name(m.group(1)), idx))
    for m in re_fork.finditer(line):
      tokens_by_func[current_func].append((_normalize_name(m.group(1)), idx))
    for m in re_join.finditer(line):
      tokens_by_func[current_func].append((_normalize_name(m.group(1)), idx))
    for m in re_back_to_decision.finditer(line):
      decision_name = _normalize_name(m.group(1))
      tokens_by_func[current_func].append((f"BackToDecision:{decision_name}", idx))

    if line.strip().startswith("function "):
      continue

    for m in re_call.finditer(line):
      tokens_by_func[current_func].append((_normalize_name(m.group(1)), idx))

  return tokens_by_func, func_decl_line


def _expand_tokens(func_name: str, tokens_by_func: Dict[str, List[Tuple[str, int]]], stack: List[str]) -> List[Tuple[str, int]]:
  if func_name in stack:
    return []
  stack.append(func_name)
  expanded: List[Tuple[str, int]] = []
  for tok, line in tokens_by_func.get(func_name, []):
    expanded.append((tok, line))
    # Inline helper function bodies at call sites (by function name match)
    if tok in tokens_by_func and tok != func_name:
      expanded.extend(_expand_tokens(tok, tokens_by_func, stack))
  stack.pop()
  return expanded


def build_order_streams(code_text: str, initial_node_ids: Set[str], final_node_ids: Set[str]) -> Dict[str, List[Tuple[str, int]]]:
  tokens_by_func, func_decl_line = _parse_functions(code_text)

  main_funcs = [
    fn for fn in tokens_by_func.keys()
    if fn.startswith("main_") or fn.startswith("main")
  ]
  streams: Dict[str, List[Tuple[str, int]]] = {}

  for main_fn in main_funcs:
    stream: List[Tuple[str, int]] = []

    # Treat IR InitialNode as entry of main function
    entry_line = func_decl_line.get(main_fn, 1)
    for init_id in initial_node_ids:
      stream.append((init_id, entry_line))

    stream.extend(_expand_tokens(main_fn, tokens_by_func, []))

    # Fallback: include helper function bodies as well (in case they are not directly expanded
    # due to call-site formatting differences). This helps validate edges that traverse
    # FlowJoin-style helper functions.
    for fn in sorted(tokens_by_func.keys()):
      if fn == "<global>" or fn == main_fn:
        continue
      if fn.startswith("main_") or fn.startswith("main"):
        continue
      stream.extend(_expand_tokens(fn, tokens_by_func, []))

    # Treat IR ActivityFinalNode as end() call
    # If code uses end(), map each final node id to the first end() occurrence in main stream.
    end_lines = [ln for tok, ln in stream if tok == "end"]
    end_line = end_lines[0] if end_lines else (stream[-1][1] if stream else entry_line)
    for fin_id in final_node_ids:
      stream.append((fin_id, end_line))

    streams[main_fn] = stream

  return streams


def build_reference_sets(diagram_ir: Dict[str, Any]) -> Tuple[Set[str], Set[str], Set[str], Set[str], Set[str], Set[str], Set[str], List[Dict[str, Any]]]:
  actions: Set[str] = set()
  decisions: Set[str] = set()
  merge_nodes: Set[str] = set()
  forks: Set[str] = set()
  joins: Set[str] = set()
  initial_nodes: Set[str] = set()
  final_nodes: Set[str] = set()
  edges: List[Dict[str, Any]] = list(diagram_ir.get("edges", []))

  for n in diagram_ir.get("nodes", []):
    t = n.get("type") or ""
    semantic_t = n.get("semanticType") or t
    action = n.get("action")
    if not action:
      continue

    if "CallBehaviorAction" in t:
      actions.add(_normalize_name(action))

    if "InitialNode" in t:
      initial_nodes.add(_normalize_name(action))

    if "ActivityFinalNode" in t:
      final_nodes.add(_normalize_name(action))

    if "DecisionNode" in t:
      decisions.add(_normalize_name(action))

    if "MergeNode" in t and semantic_t == "uml:MergeNode":
      merge_nodes.add(_normalize_name(action))

    if "ForkNode" in t:
      forks.add(_normalize_name(action))

    if "JoinNode" in t:
      joins.add(_normalize_name(action))

  return actions, decisions, merge_nodes, forks, joins, initial_nodes, final_nodes, edges


def detect_loopbacks(actions: Set[str], decisions: Set[str], edges: List[Dict[str, Any]]) -> Set[Tuple[str, str]]:
  decision_to_next: Dict[str, Set[str]] = {}
  for e in edges:
    src = _normalize_name(e.get("from") or "")
    tgt = _normalize_name(e.get("to") or "")
    if src in decisions and tgt:
      decision_to_next.setdefault(src, set()).add(tgt)

  loopbacks: Set[Tuple[str, str]] = set()
  for e in edges:
    src = _normalize_name(e.get("from") or "")
    tgt = _normalize_name(e.get("to") or "")
    if not src or not tgt:
      continue
    if tgt in decisions and src in actions:
      if src in decision_to_next.get(tgt, set()):
        loopbacks.add((src, tgt))

  return loopbacks


def check_edge_order(edges: List[Dict[str, Any]], positions: Dict[str, int], loopbacks: Set[Tuple[str, str]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
  violations: List[Dict[str, Any]] = []
  skipped: List[Dict[str, Any]] = []

  for e in edges:
    raw_from = e.get("from") or ""
    raw_to = e.get("to") or ""
    src = _normalize_name(raw_from)
    tgt = _normalize_name(raw_to)
    if not src or not tgt:
      continue

    if (src, tgt) in loopbacks:
      skipped.append({
        "from": src,
        "to": tgt,
        "reason": "loopback_edge",
      })
      continue

    src_pos = positions.get(src)
    tgt_pos = positions.get(tgt)

    if src_pos is None or tgt_pos is None:
      skipped.append({
        "from": src,
        "to": tgt,
        "reason": "node_not_found_in_code",
        "from_found": src_pos is not None,
        "to_found": tgt_pos is not None,
      })
      continue

    if src_pos > tgt_pos:
      violations.append({
        "from": src,
        "to": tgt,
        "from_line": src_pos,
        "to_line": tgt_pos,
      })

  return violations, skipped


def check_edge_order_on_streams(edges: List[Dict[str, Any]], streams: Dict[str, List[Tuple[str, int]]], loopbacks: Set[Tuple[str, str]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
  violations: List[Dict[str, Any]] = []
  skipped: List[Dict[str, Any]] = []

  # Build per-stream token index maps: token -> list of (index, line)
  index_maps: Dict[str, Dict[str, List[Tuple[int, int]]]] = {}
  for fn, stream in streams.items():
    m: Dict[str, List[Tuple[int, int]]] = {}
    for i, (tok, line) in enumerate(stream):
      m.setdefault(tok, []).append((i, line))
    index_maps[fn] = m

  for e in edges:
    src = _normalize_name(e.get("from") or "")
    tgt = _normalize_name(e.get("to") or "")
    if not src or not tgt:
      continue

    is_loopback = (src, tgt) in loopbacks
    loopback_token = f"BackToDecision:{tgt}"

    found_any_pair = False
    satisfied = False
    best: Dict[str, Any] | None = None

    for fn, m in index_maps.items():
      srcs = m.get(src)
      tgts = m.get(tgt)
      if not srcs or not tgts:
        continue
      found_any_pair = True

      if is_loopback:
        # For loopback edges (src -> decision), the decision typically appears earlier.
        # We consider it valid if there is a BackToDecision:<decision> marker after src.
        btd = m.get(loopback_token)
        if btd and min(i for i, _ in srcs) < max(i for i, _ in btd):
          satisfied = True
          break
      else:
        if min(i for i, _ in srcs) < max(i for i, _ in tgts):
          satisfied = True
          break

      best = {
        "function": fn,
        "from": src,
        "to": tgt,
        "from_lines": sorted({ln for _, ln in srcs}),
        "to_lines": sorted({ln for _, ln in tgts}),
      }

      if is_loopback:
        btd = m.get(loopback_token)
        best["required_marker"] = loopback_token
        best["marker_lines"] = sorted({ln for _, ln in btd}) if btd else []
        best["reason"] = "missing_back_to_decision_marker"

    if not found_any_pair:
      any_src = any(src in m for m in index_maps.values())
      any_tgt = any(tgt in m for m in index_maps.values())
      skipped.append({
        "from": src,
        "to": tgt,
        "reason": "node_not_found_in_code",
        "from_found": any_src,
        "to_found": any_tgt,
      })
      continue

    if not satisfied:
      violations.append(best or {"from": src, "to": tgt})

  return violations, skipped


def check_edge_order_by_function(edges: List[Dict[str, Any]], positions_by_func: Dict[str, Dict[str, List[int]]], loopbacks: Set[Tuple[str, str]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
  violations: List[Dict[str, Any]] = []
  skipped: List[Dict[str, Any]] = []

  for e in edges:
    src = _normalize_name(e.get("from") or "")
    tgt = _normalize_name(e.get("to") or "")
    if not src or not tgt:
      continue

    if (src, tgt) in loopbacks:
      skipped.append({
        "from": src,
        "to": tgt,
        "reason": "loopback_edge",
      })
      continue

    found_any_pair = False
    satisfied_in_any_function = False

    best_counterexample: Dict[str, Any] | None = None

    for func_name, positions in positions_by_func.items():
      src_positions = positions.get(src)
      tgt_positions = positions.get(tgt)
      if not src_positions or not tgt_positions:
        continue

      found_any_pair = True
      # Edge is satisfied if any src occurrence is before any tgt occurrence.
      if min(src_positions) < max(tgt_positions):
        satisfied_in_any_function = True
        break

      # Keep an example counterexample (closest lines) for reporting.
      best_counterexample = {
        "function": func_name,
        "from": src,
        "to": tgt,
        "from_lines": src_positions,
        "to_lines": tgt_positions,
      }

    if not found_any_pair:
      # If either node never appears in any function, skip (order cannot be evaluated).
      any_src = any(src in p for p in positions_by_func.values())
      any_tgt = any(tgt in p for p in positions_by_func.values())
      skipped.append({
        "from": src,
        "to": tgt,
        "reason": "node_not_found_in_code",
        "from_found": any_src,
        "to_found": any_tgt,
      })
      continue

    if not satisfied_in_any_function:
      # Both nodes exist in at least one function, but ordering is impossible in all such functions.
      violations.append(best_counterexample or {"from": src, "to": tgt})

  return violations, skipped


def main():
  diagram_ir_path = resolve_path("files/activity_parsed/5_diagram_ir.json")
  code_path = resolve_path("files/code/generated_plain_code.txt")

  diagram_ir = _load_json(diagram_ir_path)
  with open(code_path, "r", encoding="utf-8") as f:
    code_text = f.read()

  actions, decisions, merge_nodes, forks, joins, initial_nodes, final_nodes, edges = build_reference_sets(diagram_ir)

  loopbacks = detect_loopbacks(actions, decisions, edges)

  streams = build_order_streams(code_text, initial_nodes, final_nodes)
  violations, skipped = check_edge_order_on_streams(edges, streams, loopbacks)

  report = {
    "violations": violations,
    "skipped_edges": skipped,
    "summary": {
      "total_edges": len(edges),
      "checked_edges": len(edges) - len(skipped),
      "violations_count": len(violations),
      "skipped_edges_count": len(skipped),
    },
  }

  report_path = resolve_path("reports/structural_order.json")
  os.makedirs(os.path.dirname(report_path), exist_ok=True)
  with open(report_path, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)

  print(f"Validation report saved to {report_path}")

  if report["violations"]:
    print("ERROR: Order violations detected!")


if __name__ == "__main__":
  main()
