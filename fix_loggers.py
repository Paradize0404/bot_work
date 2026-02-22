import ast
import glob
import os


def fix_file(filepath):
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    tree = ast.parse(content)
    lines = content.split("\n")

    # We need to process nodes in reverse order of their line numbers
    # so that inserting lines doesn't mess up the line numbers of subsequent nodes
    nodes_to_fix = []

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            args = [arg.arg for arg in node.args.args]
            if "message" in args or "callback" in args:
                has_logger = False
                for stmt in node.body[:5]:
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                        if isinstance(stmt.value.func, ast.Attribute):
                            if (
                                isinstance(stmt.value.func.value, ast.Name)
                                and stmt.value.func.value.id == "logger"
                            ):
                                has_logger = True
                if not has_logger:
                    nodes_to_fix.append(node)

    nodes_to_fix.sort(key=lambda n: n.lineno, reverse=True)

    for node in nodes_to_fix:
        args = [arg.arg for arg in node.args.args]
        insert_line = node.body[0].lineno - 1
        if (
            isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            insert_line = node.body[0].end_lineno

        indent = " " * node.body[0].col_offset

        module_name = os.path.basename(filepath).replace(".py", "")

        if "message" in args:
            logger_stmt = f'{indent}logger.info("[{module_name}] {node.name} tg:%d", message.from_user.id)'
        else:
            logger_stmt = f'{indent}logger.info("[{module_name}] {node.name} tg:%d", callback.from_user.id)'

        lines.insert(insert_line, logger_stmt)

    with open(filepath, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))


for f in glob.glob("bot/*_handlers.py"):
    fix_file(f)
    print(f"Fixed {f}")
