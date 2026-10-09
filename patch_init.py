"""Patch __init__.py: export scan_skills_dirs."""
import sys

p = r"smartbutler\thinking\skills\__init__.py"
with open(p, encoding="utf-8") as f:
    text = f.read()

# 1. import 块加 scan_skills_dirs
old1 = "    parse_skill_md,\n    scan_skills_dir,\n)"
new1 = "    parse_skill_md,\n    scan_skills_dir,\n    scan_skills_dirs,\n)"
assert old1 in text, "old1 not found"
text = text.replace(old1, new1, 1)

# 2. __all__ 块加 scan_skills_dirs
old2 = '    "scan_skills_dir",\n]'
new2 = '    "scan_skills_dir",\n    "scan_skills_dirs",\n]'
assert old2 in text, "old2 not found"
text = text.replace(old2, new2, 1)

with open(p, "w", encoding="utf-8") as f:
    f.write(text)
print("OK")
