import os
import sys
import argparse
import subprocess
import shutil


def run(cmd: list[str]) -> int:
  print("$", " ".join(cmd))
  proc = subprocess.run(cmd)
  return proc.returncode


def main():
  parser = argparse.ArgumentParser(description="Run pipeline validations: JSON structures and plain vs target code")
  parser.add_argument("--base", default=os.path.join("diagram-parser", "files"), help="Base folder for files")
  parser.add_argument("--lang", default="auto", help="Target language (Python|Java|auto)")
  parser.add_argument("--flow", default=os.path.join("diagram-parser", "files", "activity_parsed", "2_activity_flow.json"), help="Path to 2_activity_flow.json")
  parser.add_argument("--plain", default=os.path.join("diagram-parser", "files", "code", "generated_plain_code.txt"), help="Path to generated_plain_code.txt")
  parser.add_argument("--target", default=os.path.join("diagram-parser", "files", "ai_code", "generated_code_target_language.txt"), help="Path to generated target code")
  parser.add_argument("--report", default=os.path.join("diagram-parser", "reports", "report.json"), help="Path to write validation report JSON")
  parser.add_argument("--strict-lang", action="store_true", help="Do not auto-detect/fallback language in plain-vs-target validator")
  args = parser.parse_args()

  # Ensure reports directory
  reports_dir = os.path.join("diagram-parser", "reports")
  os.makedirs(reports_dir, exist_ok=True)

  # JSON validations (best-effort: validate files if exist)
  json_files = [
    ("1", os.path.join(args.base, "activity_parsed", "1_activity_with_conditions.json")),
    ("2", os.path.join(args.base, "activity_parsed", "2_activity_flow.json")),
    ("3", os.path.join(args.base, "activity_parsed", "3_activity_with_steps.json")),
    ("4", os.path.join(args.base, "activity_parsed", "4_activity_optimized.json")),
    ("5", os.path.join(args.base, "activity_parsed", "5_diagram_ir.json")),
  ]

  overall_rc = 0
  for t, path in json_files:
    if os.path.exists(path):
      rc = run([sys.executable, os.path.join("diagram-parser/static_validator", "validate_schema_jsons.py"), "--type", t, "--file", path])
      overall_rc = overall_rc or rc
    else:
      print(f"(skip) Missing JSON: {path}")

  # Run structural and semantic validators
  rc = run([sys.executable, os.path.join("diagram-parser", "static_validator", "structural.py")])
  overall_rc = overall_rc or rc
  # run([sys.executable, os.path.join("diagram-parser", "static_validator", "structural_ai.py")])
  rc = run([sys.executable, os.path.join("diagram-parser", "static_validator", "full_structural.py")])
  overall_rc = overall_rc or rc
  rc = run([sys.executable, os.path.join("diagram-parser", "static_validator", "structural_order.py")])
  overall_rc = overall_rc or rc

  # Move any known report files into reports dir
  candidates = [
    os.path.join("diagram-parser", "report_structural.json"),
    os.path.join("diagram-parser", "report_structural_ai.json"),
    os.path.join("diagram-parser", "report_semantic.json"),
    os.path.join("diagram-parser", "report_semantic_stats.json"),
    os.path.join("diagram-parser", "static_validator", "report_structural.json"),
    os.path.join("diagram-parser", "static_validator", "report_structural_ai.json"),
    os.path.join("diagram-parser", "static_validator", "report_semantic.json"),
    os.path.join("diagram-parser", "static_validator", "report_semantic_stats.json"),
  ]
  for src in candidates:
    if os.path.exists(src):
      dst = os.path.join(reports_dir, os.path.basename(src))
      try:
        shutil.move(src, dst)
        print(f"Moved {src} -> {dst}")
      except Exception as e:
        print(f"Could not move {src}: {e}")

  if overall_rc != 0:
    print("VALIDATIONS FAILED")
  else:
    print("ALL VALIDATIONS PASSED")
  sys.exit(overall_rc)


if __name__ == "__main__":
  main()
