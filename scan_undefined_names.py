"""穷尽扫描：局部懒加载 import 与模块级 import 遗漏导致的 NameError 风险。

规则：某个函数体内使用了 `px.` / `go.` / `np.` / `pd.` 等模块别名，
但该函数作用域内既无对应 import，模块顶层也无对应 import -> 运行时会 NameError。
"""
import ast
import os
import sys
from collections import defaultdict

# 别名 -> 常见缺失提示
WATCH = {
    "px": "plotly.express",
    "go": "plotly.graph_objects",
    "np": "numpy",
    "pd": "pandas",
    "plt": "matplotlib.pyplot",
    "sns": "seaborn",
    "re": "re",
    "json": "json",
    "os": "os",
    "sys": "sys",
    "time": "time",
    "datetime": "datetime",
    "Path": "pathlib.Path",
    "sqlite3": "sqlite3",
    "requests": "requests",
}


def iter_module_level_stmts(stmts):
    """迭代模块级语句，递归进入 if/try/with 的 body，但不进入 def/class 内部。"""
    for stmt in stmts:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, field, None)
            if isinstance(inner, list) and inner:
                yield from iter_module_level_stmts(inner)
        for handler in getattr(stmt, "handlers", []) or []:
            yield from iter_module_level_stmts(handler.body)


def collect_import_aliases(tree):
    """只收集模块顶层(含 if/try 块内)的 import 别名，不含函数内懒加载 import。"""
    names = set()
    for node in iter_module_level_stmts(tree.body):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
    return names


def collect_module_level_names(tree):
    """模块级定义的函数/类/全局变量名(视为可用)。"""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.If, ast.Try)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
    return names


def own_scope_nodes(func_node):
    """产出函数自身作用域内的所有节点，不进入嵌套 def/class 内部。

    关键：嵌套函数里的 import 属于嵌套函数的局部变量，不能救外层函数。
    """
    stack = list(func_node.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def func_local_names(func_node):
    """函数自身作用域内所有被绑定/导入的名字(不含嵌套函数内部)。"""
    names = set()
    for node in own_scope_nodes(func_node):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for t in ast.walk(node.optional_vars):
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.comprehension):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    names.add(t.id)
    for arg in (func_node.args.args + func_node.args.kwonlyargs
                + func_node.args.posonlyargs):
        names.add(arg.arg)
    return names


def check_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
    except (SyntaxError, UnicodeDecodeError) as e:
        return [("PARSE_ERROR", 0, str(e))]

    module_imports = collect_import_aliases(tree)
    module_names = collect_module_level_names(tree)
    module_available = module_imports | module_names

    issues = []

    def walk_funcs(node, parents_available):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local = func_local_names(child)
                available = parents_available | local
                # 检查该函数自身作用域(不含嵌套函数)中引用的模块别名
                used = set()
                for sub in own_scope_nodes(child):
                    if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                        used.add(sub.value.id)
                for name in sorted(used):
                    if name in WATCH and name not in available:
                        issues.append((name, child.lineno, f"func {child.name}"))
                walk_funcs(child, available)
            elif isinstance(child, ast.ClassDef):
                walk_funcs(child, parents_available)

    walk_funcs(tree, module_available)
    return issues


def main(root):
    total = 0
    skip_dirs = {".git", "__pycache__", ".venv", "node_modules", ".persist"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            issues = check_file(path)
            if issues:
                total += len(issues)
                rel = os.path.relpath(path, root)
                for name, lineno, where in issues:
                    print(f"{rel}:{lineno}  [{where}]  '{name}' 未定义 (建议 {WATCH.get(name, '?')})")
    print(f"\n共发现 {total} 处潜在未定义引用")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
