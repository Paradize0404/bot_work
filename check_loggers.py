import os, ast

for root, dirs, files in os.walk('bot'):
    for file in files:
        if file.endswith('.py'):
            path = os.path.join(root, file)
            with open(path, 'r', encoding='utf-8-sig') as f:
                content = f.read()
            
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    args = [arg.arg for arg in node.args.args]
                    if 'message' in args or 'callback' in args:
                        has_logger = False
                        for stmt in node.body[:5]:
                            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                                if isinstance(stmt.value.func, ast.Attribute):
                                    if isinstance(stmt.value.func.value, ast.Name) and stmt.value.func.value.id == 'logger':
                                        has_logger = True
                        if not has_logger:
                            print(f'{path}: {node.name} missing logger')
print("Done checking loggers")

