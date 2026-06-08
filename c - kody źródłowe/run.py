import os
import json
import argparse
import importlib.util


def import_module_from_path(name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  assert spec and spec.loader
  spec.loader.exec_module(module)
  return module


def ensure_dir(path):
  d = os.path.dirname(path)
  if d:
    os.makedirs(d, exist_ok=True)


def main():
  parser = argparse.ArgumentParser(description="Orchestrate UML -> JSONs -> Pseudocode -> LLM code")
  parser.add_argument("--uml", default="diagram-parser/files/xmi_uml/9.uml")
  parser.add_argument("--activity-index", type=int, default=0)
  parser.add_argument("--activity-name")
  parser.add_argument("--optimize", action="store_true", help="Use steps optimization stage (4b)")
  parser.add_argument("--enrich", action="store_true", help="Run LLM enrichment")
  parser.add_argument("--model", default="codellama")
  args = parser.parse_args()

  base = "diagram-parser/files"
  out1 = os.path.join(base, "activity_parsed/1_activity_with_conditions.json")
  out2 = os.path.join(base, "activity_parsed/2_activity_flow.json")
  out3 = os.path.join(base, "activity_parsed/3_activity_with_steps.json")
  out4 = os.path.join(base, "activity_parsed/4_activity_optimized.json")
  plain_path = os.path.join(base, "code/generated_plain_code.txt")
  target_path = os.path.join(base, "ai_code/generated_code_target_language.txt")

  # Step 1: read UML (non-interactive)
  mod1b = import_module_from_path("mod1b", os.path.join("diagram-parser", "1_read_uml_xmi_cli.py"))
  model1 = mod1b.parse_visual_paradigm_activity(args.uml, args.activity_index, args.activity_name)
  ensure_dir(out1)
  with open(out1, "w", encoding="utf-8") as f:
    json.dump(model1, f, ensure_ascii=False, indent=2)

  # Step 2: format to flow
  mod2 = import_module_from_path("mod2", os.path.join("diagram-parser", "2_format_to_flow.py"))
  transformed2 = mod2.transform_flow(model1)
  with open(out2, "w", encoding="utf-8") as f:
    json.dump(transformed2, f, ensure_ascii=False, indent=2)

  # Step 3: flow to steps
  mod3 = import_module_from_path("mod3", os.path.join("diagram-parser", "3_flow_to_steps.py"))
  transformed3 = mod3.transform(transformed2)
  with open(out3, "w", encoding="utf-8") as f:
    json.dump(transformed3, f, ensure_ascii=False, indent=2)

  # Step 3b: Build diagram IR
  out5 = os.path.join(base, "activity_parsed/5_diagram_ir.json")
  mod5_ir = import_module_from_path("mod5_ir", os.path.join("diagram-parser", "5_build_diagram_ir.py"))
  with open(out1, "r", encoding="utf-8") as f:
    model1 = json.load(f)
  ir_result = mod5_ir.build_ir(model1)
  with open(out5, "w", encoding="utf-8") as f:
    json.dump(ir_result, f, ensure_ascii=False, indent=2)
  print(f"Saved diagram IR to {out5}")

  # Step 3c: Generate graph visualizations from IR
  out6_dot = os.path.join(base, "activity_parsed/6_diagram_ir.dot")
  out6_mmd = os.path.join(base, "activity_parsed/6_diagram_ir.mmd")
  mod6_graph = import_module_from_path("mod6_graph", os.path.join("diagram-parser", "6_diagram_ir_to_graph.py"))
  with open(out5, "r", encoding="utf-8") as f:
    ir_model = json.load(f)
  
  # Generate DOT file
  dot_content = mod6_graph.to_dot(ir_model)
  with open(out6_dot, "w", encoding="utf-8") as f:
    f.write(dot_content)
  
  # Generate Mermaid file
  mermaid_content = mod6_graph.to_mermaid(ir_model)
  with open(out6_mmd, "w", encoding="utf-8") as f:
    f.write(mermaid_content)
  
  print(f"Saved graph visualizations to {out6_dot} and {out6_mmd}")

  # Step 4 (optional): optimize paths (use our fixed version)
  if args.optimize:
    mod4b = import_module_from_path("mod4b", os.path.join("diagram-parser", "4_steps_to_paths.py"))
    groups = mod4b.group_by_suffix(transformed3["steps"])  # type: ignore
    optimized_flow = mod4b.build_optimized_flow(groups)
    data_clean = mod4b.remove_condition_from_actions(optimized_flow)
    result4 = {"activity": transformed3["activity"], "flow": data_clean}
    with open(out4, "w", encoding="utf-8") as f:
      json.dump(result4, f, ensure_ascii=False, indent=2)

  # Step 5: generate pseudocode from flow (use step2 flow)
  mod5 = import_module_from_path("mod5_pseudo", os.path.join("diagram-parser", "5_generate_plain_code.py"))
  pseudo = mod5.generate_pseudocode(transformed2)
  ensure_dir(plain_path)
  with open(plain_path, "w", encoding="utf-8") as f:
    f.write(pseudo)

  # Step 6 (optional): Enrich with LLM (non-interactive)
  if args.enrich:
    mod10b = import_module_from_path("mod10b", os.path.join("diagram-parser", "10_generate_ai_code.py"))
    mod10b.main = getattr(mod10b, 'main')  # ensure available
    # Call via function interface
    import sys
    saved = sys.argv
    sys.argv = [saved[0], "--input", plain_path, "--output", target_path, "--model", args.model]
    try:
      mod10b.main()
    finally:
      sys.argv = saved

  print("Pipeline finished.")


if __name__ == "__main__":
  main()
