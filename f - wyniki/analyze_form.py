import csv
import re
import sys
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# MAPOWANIE ODPOWIEDZI
# ---------------------------------------------------------------------------

ODPOWIEDZI_MAP = {
  "zdecydowanie tak": 5,
  "raczej tak": 4,
  "częściowo": 3,
  "raczej nie": 2,
  "zdecydowanie nie": 1,
}

DISPLAY_ORDER = [
  "Zdecydowanie tak",
  "Raczej tak",
  "Częściowo",
  "Raczej nie",
  "Zdecydowanie nie",
]

# ---------------------------------------------------------------------------
# KRYTERIA
# ---------------------------------------------------------------------------

SCENARIO_CRITERIA = [
  "Czy wszystkie istotne kroki procesu zostały odwzorowane w kodzie",
  "Czy przepływ sterowania odpowiada diagramowi UML",
  "Czy zachowano poprawną kolejność operacji i zależności między akcjami",
  "Czy stereotypy oraz metadane zostały poprawnie odwzorowane",
  "Czy wygenerowany kod przypomina kod możliwy do dalszego rozwoju produkcyjnego",
  "Czy metakod ułatwia analizę zgodności",
]

GENERAL_CRITERIA = [
  "Czy zastosowanie pośredniej specyfikacji",
  "Czy metaatrybuty (@meta)",
  "Czy przedstawione podejście może usprawnić proces implementacji serwisów REST",
  "Czy metoda wydaje się możliwa do zastosowania w bardziej złożonych systemach",
]

SCENARIOS = ["S1", "S2", "S3", "S4"]

# ---------------------------------------------------------------------------
# NORMALIZACJA
# ---------------------------------------------------------------------------

def normalize(text):

  if text is None:
    return ""

  text = text.lower()

  text = re.sub(r"\(.*?\)", " ", text)
  text = re.sub(r"[\[\]\?]", " ", text)
  text = re.sub(r"\s+", " ", text)

  return text.strip()


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def load_csv(path):

  with open(path, newline="", encoding="utf-8-sig") as f:

    reader = csv.reader(f)

    rows = list(reader)

  headers = rows[0]
  data = rows[1:]

  return headers, data


# ---------------------------------------------------------------------------
# WYSZUKIWANIE KOLUMN
# ---------------------------------------------------------------------------

def find_scenario_columns(headers):

  scenario_columns = defaultdict(list)

  matched_columns = []

  for idx, header in enumerate(headers):

    normalized = normalize(header)

    for criterion in SCENARIO_CRITERIA:

      if normalize(criterion) in normalized:
        matched_columns.append(idx)
        break

  expected = len(SCENARIOS) * len(SCENARIO_CRITERIA)

  if len(matched_columns) != expected:

    print(f"UWAGA: znaleziono {len(matched_columns)} kolumn pytań")
    print(f"Oczekiwano: {expected}")

  pos = 0

  for scenario in SCENARIOS:

    scenario_columns[scenario] = matched_columns[
                                 pos:pos + len(SCENARIO_CRITERIA)
                                 ]

    pos += len(SCENARIO_CRITERIA)

  return scenario_columns


def find_general_columns(headers):

  result = {}

  for criterion in GENERAL_CRITERIA:

    crit_norm = normalize(criterion)

    for idx, header in enumerate(headers):

      if crit_norm in normalize(header):
        result[criterion] = idx
        break

  return result


# ---------------------------------------------------------------------------
# ANALIZA KOLUMNY
# ---------------------------------------------------------------------------

def analyze_column(data, col_idx):

  scores = []

  positive = 0
  neutral = 0
  negative = 0

  counts = defaultdict(int)

  for row in data:

    if col_idx >= len(row):
      continue

    value = row[col_idx].strip()

    if not value:
      continue

    counts[value] += 1

    key = value.lower()

    score = ODPOWIEDZI_MAP.get(key)

    if score is None:
      continue

    scores.append(score)

    if score >= 4:
      positive += 1
    elif score == 3:
      neutral += 1
    else:
      negative += 1

  total = len(scores)

  mean = sum(scores) / total if total else 0

  return {
    "scores": scores,
    "mean": mean,
    "positive": positive,
    "neutral": neutral,
    "negative": negative,
    "total": total,
    "counts": dict(counts),
  }

# ---------------------------------------------------------------------------
# FORMATOWANIE
# ---------------------------------------------------------------------------

def format_dist(counts):

  parts = []

  for label in DISPLAY_ORDER:

    n = counts.get(label, 0)

    if n:
      parts.append(f"{label}: {n}")

  return " | ".join(parts)


# ---------------------------------------------------------------------------
# MAIN ANALIZA
# ---------------------------------------------------------------------------

def analyze(path):

  headers, data = load_csv(path)

  print(f"\nWczytano rekordów: {len(data)}")
  print(f"Plik: {path}")

  scenario_columns = find_scenario_columns(headers)

  # -----------------------------------------------------------------------
  # TABELA 20
  # -----------------------------------------------------------------------

  print("\n" + "=" * 80)
  print("TABELA 20 – Wyniki per scenariusz")
  print("=" * 80)

  for scenario in SCENARIOS:

    all_scores = []

    positive = 0
    neutral = 0
    negative = 0

    counts = defaultdict(int)

    for col_idx in scenario_columns[scenario]:

      result = analyze_column(data, col_idx)

      positive += result["positive"]
      neutral += result["neutral"]
      negative += result["negative"]

      for k, v in result["counts"].items():
        counts[k] += v

      all_scores.extend(result["scores"])

    total = len(all_scores)

    mean = sum(all_scores) / total if total else 0

    print(f"\n{scenario}")
    print(f"  Średnia ocena:    {mean:.2f}")
    print(f"  Pozytywne:        {positive}/{total}")
    print(f"  Neutralne:        {neutral}/{total}")
    print(f"  Negatywne:        {negative}/{total}")
    print(f"  Rozkład:          {format_dist(counts)}")

  # -----------------------------------------------------------------------
  # CZĘŚĆ OGÓLNA
  # -----------------------------------------------------------------------

  print("\n" + "=" * 80)
  print("CZĘŚĆ OGÓLNA")
  print("=" * 80)

  general_columns = find_general_columns(headers)

  for criterion, col_idx in general_columns.items():

    result = analyze_column(data, col_idx)

    print(f"\n{headers[col_idx]}")
    print(f"  Średnia ocena:    {result['mean']:.2f}")
    print(f"  Pozytywne:        {result['positive']}/{result['total']}")
    print(f"  Neutralne:        {result['neutral']}/{result['total']}")
    print(f"  Negatywne:        {result['negative']}/{result['total']}")
    print(f"  Rozkład:          {format_dist(result['counts'])}")


# ---------------------------------------------------------------------------
# START
# ---------------------------------------------------------------------------

if __name__ == "__main__":

  csv_path = sys.argv[1] if len(sys.argv) > 1 else "wyniki.csv"

  if not Path(csv_path).exists():

    print(f"Błąd: plik '{csv_path}' nie istnieje.")
    sys.exit(1)

  analyze(csv_path)