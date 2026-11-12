"""Turn unittest failures in a saved log into GitHub error annotations.

Annotations show on the run page and in the public API without signing in,
unlike the full job log.
"""
import re
import sys


def escape(text):
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main(path):
    log = open(path, encoding="utf-8", errors="replace").read()
    # unittest prints each problem as a "=====" line, "ERROR: name" or "FAIL: name", then the traceback.
    blocks = re.split(r"^={50,}$", log, flags=re.M)[1:]
    for block in blocks[:10]:
        lines = block.strip().splitlines()
        if not lines:
            continue
        title = lines[0].strip()
        body = "\n".join(line for line in lines[1:] if not set(line) <= {"-"})
        print(f"::error title={escape(title)}::{escape(body[-3500:])}")
    if not blocks:
        print(f"::error title=Tests failed::{escape(log[-3500:])}")


if __name__ == "__main__":
    main(sys.argv[1])
