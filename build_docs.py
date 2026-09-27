#!/usr/bin/env python3
"""
Render README.md into a static site for GitHub Pages.

Kept as a script rather than a long inline shell command in the workflow so the
exact same build can be run locally before pushing:

    python3 build_docs.py            # writes _site/
    python3 build_docs.py --serve    # build, then serve on :8000 to preview

Deliberately dependency-free: pandoc is the only requirement, and it ships with
the GitHub-hosted runners as well as being a single `brew install pandoc` away
locally. The output goes to _site/, which .gitignore excludes, so a local build
never gets committed by accident.
"""

import argparse
import http.server
import shutil
import socketserver
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
README = BASE_DIR / "README.md"
SITE = BASE_DIR / "_site"
STYLE = BASE_DIR / "docs" / "style.css"

# pandoc -s (standalone) emits its own <html>/<head>; without --template that
# gives a bare page, so supply one with a title and the stylesheet link.
TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title$</title>
<meta name="description" content="$description$">
<link rel="stylesheet" href="style.css">
</head>
<body>
$body$
</body>
</html>
"""


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def check_pandoc():
    if shutil.which("pandoc") is None:
        fail("pandoc not found. Install it with: brew install pandoc")


def build():
    if not README.exists():
        fail(f"missing {README.name}")

    check_pandoc()
    SITE.mkdir(exist_ok=True)

    # The README's first line is the H1; use it as the page title so the two
    # cannot drift apart.
    first_line = ""
    for line in README.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            first_line = line[2:].strip()
            break
    if not first_line:
        fail(f"{README.name} does not start with a '# ' heading")

    template_path = SITE / ".template.html"
    template_path.write_text(TEMPLATE, encoding="utf-8")

    result = subprocess.run(
        [
            "pandoc",
            str(README),
            "--from=gfm",           # GitHub-flavoured markdown: tables, fenced code
            "--to=html5",
            "--standalone",
            f"--template={template_path}",
            "--metadata", f"title={first_line}",
            "--metadata", "description=Unfollow every account you follow, via the private app API.",
            "-o", str(SITE / "index.html"),
        ],
        capture_output=True,
        text=True,
    )
    template_path.unlink(missing_ok=True)

    if result.returncode != 0:
        fail(f"pandoc failed:\n{result.stderr.strip()}")

    shutil.copy(STYLE, SITE / "style.css")

    size = (SITE / "index.html").stat().st_size
    print(f"built {SITE / 'index.html'} ({size:,} bytes) from {README.name}")
    print(f"title: {first_line}")


def serve(port):
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        os_cwd = Path.cwd()
        import os
        os.chdir(SITE)
        print(f"serving {SITE} at http://localhost:{port}  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
        finally:
            os.chdir(os_cwd)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve", action="store_true",
                        help="serve the built site locally to preview it")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    build()
    if args.serve:
        serve(args.port)


if __name__ == "__main__":
    main()
