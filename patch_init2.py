p = r"smartbutler\thinking\skills\__init__.py"
with open(p, encoding="utf-8") as f:
    text = f.read()

old2 = '    "scan_skills_dir",\n'
# 必须唯一(只在 __all__ 出现一次)——检查
assert text.count(old2) == 1, f"count = {text.count(old2)}"
new2 = '    "scan_skills_dir",\n    "scan_skills_dirs",\n'
text = text.replace(old2, new2, 1)

with open(p, "w", encoding="utf-8") as f:
    f.write(text)
print("OK")
