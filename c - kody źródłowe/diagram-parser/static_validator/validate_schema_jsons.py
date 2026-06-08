import json
import argparse
import sys
import os
from typing import Any, Dict
import jsonschema


def load_json(path: str) -> Any:
  with open(path, "r", encoding="utf-8") as f:
    return json.load(f)


def validate_with_schema(data: Any, schema: Dict) -> None:
  """Validate data against JSON schema using jsonschema library"""
  try:
    jsonschema.validate(data, schema)
  except jsonschema.ValidationError as e:
    raise ValueError(f"JSON Schema validation failed: {e.message} (path: {' -> '.join(str(p) for p in e.absolute_path)})")
  except jsonschema.SchemaError as e:
    raise ValueError(f"Schema error: {e.message}")


def main():
  parser = argparse.ArgumentParser(description="Validate pipeline JSON structures using JSON schemas")
  parser.add_argument("--type", choices=["1", "2", "3", "4", "5"], required=True, help="Schema type")
  parser.add_argument("--file", required=True, help="Path to JSON file to validate")
  parser.add_argument("--schema-dir", default="diagram-parser/schemas", help="Directory containing schema files")
  args = parser.parse_args()

  # Load the data file
  try:
    data = load_json(args.file)
  except Exception as e:
    print(f"INVALID: Could not load JSON file: {e}")
    sys.exit(1)

  # Load the corresponding schema
  schema_mapping = {
    "1": "1_activity_with_conditions.schema.json",
    "2": "2_activity_flow.schema.json", 
    "3": "3_activity_with_steps.schema.json",
    "4": "4_activity_optimized.schema.json",
    "5": "5_diagram_ir.schema.json"
  }
  
  if args.type not in schema_mapping:
    print(f"INVALID: Unknown schema type: {args.type}")
    sys.exit(1)
    
  schema_file = os.path.join(args.schema_dir, schema_mapping[args.type])
  
  if not os.path.exists(schema_file):
    print(f"INVALID: Schema file not found: {schema_file}")
    sys.exit(1)

  try:
    schema = load_json(schema_file)
  except Exception as e:
    print(f"INVALID: Could not load schema file: {e}")
    sys.exit(1)

  # Validate the data against the schema
  try:
    validate_with_schema(data, schema)
  except ValueError as e:
    print(f"INVALID: {e}")
    sys.exit(1)

  print("VALID")


if __name__ == "__main__":
  main()
