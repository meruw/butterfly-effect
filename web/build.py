"""Genera docs/index.html (pagina independiente) a partir de web/efecto-mariposa.html."""
import os

here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "efecto-mariposa.html"), encoding="utf-8").read()
head = (
    '<!doctype html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    "<style>body{margin:0}[hidden]{display:none!important}</style>\n</head>\n<body>\n"
)
out = os.path.join(here, "..", "docs", "index.html")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8", newline="\n") as f:
    f.write(head + src + "</body>\n</html>\n")
print("escrito", os.path.normpath(out))
