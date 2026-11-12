#!/usr/bin/env python3
"""Render the vmalert rule files and stage their vmalert-tool tests for validate_configs.sh.

The rule files are Jinja templates, so vmalert-tool cannot read them from the source
tree. This writes <out_dir>/vmalert-rules/<class>.yml (rendered with the same helpers
the tests use: StrictUndefined, fake values only, estate "example") and copies
src/config/skmon/rules/tests/*_test.yml to <out_dir>/vmalert-rules/tests/, where each
test file's `rule_files: [../<class>.yml]` names its rendered sibling.

usage: render_rules.py <out_dir>
"""
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from skmon_support import RULE_CLASSES, RULES, render_rules  # noqa: E402


def main(out: pathlib.Path) -> None:
    dest = out / "vmalert-rules"
    (dest / "tests").mkdir(parents=True, exist_ok=True)
    for cls in RULE_CLASSES:
        (dest / f"{cls}.yml").write_text(render_rules(cls))
        shutil.copyfile(RULES / "tests" / f"{cls}_test.yml", dest / "tests" / f"{cls}_test.yml")
        print(f"rendered {dest / f'{cls}.yml'} and staged tests/{cls}_test.yml")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[-1])
    main(pathlib.Path(sys.argv[1]))
