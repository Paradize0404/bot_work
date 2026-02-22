import re
import os


def get_tables_from_code():
    tables = set()
    for file in ["db/models.py", "db/ft_models.py"]:
        if os.path.exists(file):
            with open(file, "r", encoding="utf-8") as f:
                content = f.read()
                matches = re.findall(
                    r'__tablename__\s*=\s*[\'"]([^\'"]+)[\'"]', content
                )
                tables.update(matches)
    return tables


def get_tables_from_docs():
    tables = set()
    if os.path.exists("docs/DATABASE.md"):
        with open("docs/DATABASE.md", "r", encoding="utf-8") as f:
            content = f.read()
            matches = re.findall(r"### \d+\.\s*`([^`]+)`", content)
            tables.update(matches)
    return tables


code_tables = get_tables_from_code()
doc_tables = get_tables_from_docs()

print("Tables in code but not in docs:")
for t in sorted(code_tables - doc_tables):
    print(f"  - {t}")

print("\nTables in docs but not in code:")
for t in sorted(doc_tables - code_tables):
    print(f"  - {t}")
